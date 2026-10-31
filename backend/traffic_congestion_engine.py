"""
Hệ Thống Đo Độ Ùn Tắc Giao Thông Đa Chiều (YOLO + ByteTrack + Spatio-Temporal Congestion Analyzer)
==================================================================================================
Kiến trúc tổng thể 7 tầng:
1. Camera giao thông: Video trực tiếp / RTSP / Ảnh tĩnh (tích hợp bộ đọc RTSP bất đồng bộ chống trễ)
2. Phát hiện và theo dõi phương tiện: YOLO + ByteTrack (Vị trí, ID, quỹ đạo của từng xe)
3. Trích xuất đặc trưng giao thông: Tốc độ · Mật độ · Lưu lượng · Chiều dài hàng chờ · Thời gian dừng
   (Tự hiệu chỉnh tỷ lệ mét/pixel theo kích thước thực của phương tiện + phối cảnh mặt đường)
4. Bộ xác định trạng thái ùn tắc: Phân tích đa chiều theo không gian và thời gian
5. Kết quả phân loại 4 trạng thái:
   - Thông thoáng
   - Lưu thông chậm
   - Ùn tắc
   - Chưa đủ dữ liệu
6. Backend + cơ sở dữ liệu: Lưu trạng thái, vị trí, thời gian và lịch sử ùn tắc vào SQLite
7. Bản đồ giao thông và cảnh báo: Hiển thị cảnh báo trực quan và bảng thông số thời gian thực
"""

import time
import math
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any

import cv2
import numpy as np
import pandas as pd
import torch

INFER_DEVICE = 0 if torch.cuda.is_available() else "cpu"


# Hệ số quy đổi xe con tiêu chuẩn (PCU - Passenger Car Unit) theo TCVN 4054 / QCVN
VEHICLE_PCU_WEIGHTS = {
    "bicycle": 0.25,
    "motorcycle": 0.30,
    "motorbike": 0.30,
    "car": 1.00,
    "bus": 2.50,
    "truck": 2.00,
}

# Chiều dài trung bình phương tiện (mét) để ước lượng chiều dài hàng chờ
VEHICLE_AVG_LENGTH_M = {
    "bicycle": 1.7,
    "motorcycle": 2.0,
    "motorbike": 2.0,
    "car": 4.5,
    "bus": 10.5,
    "truck": 7.5,
}

# Kích thước tham chiếu thực tế (mét) của BBox xe (dùng cho tự hiệu chỉnh m/pixel)
# (chiều ngang thực tế m, chiều cao thực tế m)
VEHICLE_REAL_DIMS_M = {
    "bicycle": (0.65, 1.45),
    "motorcycle": (0.75, 1.50),
    "motorbike": (0.75, 1.50),
    "car": (1.82, 1.50),
    "bus": (2.50, 3.00),
    "truck": (2.35, 2.60),
}

VEHICLE_VIET_NAMES = {
    "bicycle": "Xe dap",
    "motorcycle": "Xe may",
    "motorbike": "Xe may",
    "car": "O to",
    "bus": "Xe buyt",
    "truck": "Xe tai",
}


class LatestFrameGrabber:
    """
    Bộ đọc luồng RTSP / Camera chạy ngầm (Non-blocking Thread) giúp luôn lấy khung hình
    mới nhất từ camera, loại bỏ hoàn toàn hiện tượng giật lag do ứ đọng buffer FFmpeg.
    """

    def __init__(self, src: Any, max_width: int = 960):
        self.src = src
        self.max_width = max_width
        self.cap = cv2.VideoCapture(src)
        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        self.is_opened = self.cap.isOpened()
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        if self.fps <= 1.0 or self.fps > 120.0:
            self.fps = 25.0
        self.lock = threading.Lock()
        self.latest_frame: Optional[np.ndarray] = None
        self.latest_ts: float = 0.0
        self.frame_seq: int = 0
        self.running = False
        self.thread: Optional[threading.Thread] = None

        if self.is_opened:
            ret, frm = self.cap.read()
            if ret and frm is not None:
                if self.max_width and frm.shape[1] > self.max_width:
                    h, w = frm.shape[:2]
                    frm = cv2.resize(frm, (self.max_width, int(h * self.max_width / w)), interpolation=cv2.INTER_AREA)
                self.latest_frame = frm
                self.latest_ts = time.time()
                self.frame_seq = 1
            self.running = True
            self.thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.thread.start()

    def _reader_loop(self):
        while self.running and self.cap.isOpened():
            ret, frm = self.cap.read()
            if not ret or frm is None:
                time.sleep(0.005)
                continue
            if self.max_width and frm.shape[1] > self.max_width:
                h, w = frm.shape[:2]
                frm = cv2.resize(frm, (self.max_width, int(h * self.max_width / w)), interpolation=cv2.INTER_AREA)
            now = time.time()
            with self.lock:
                self.latest_frame = frm
                self.latest_ts = now
                self.frame_seq += 1

    def read_latest(self) -> Tuple[bool, Optional[np.ndarray], float, int]:
        with self.lock:
            if self.latest_frame is None:
                return False, None, 0.0, 0
            return True, self.latest_frame.copy(), self.latest_ts, self.frame_seq

    def release(self):
        self.running = False
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=0.4)
        try:
            self.cap.release()
        except Exception:
            pass


@dataclass
class TrafficCongestionReport:
    status_label: str          # "Thông thoáng" | "Lưu thông chậm" | "Ùn tắc" | "Chưa đủ dữ liệu"
    status_code: str           # "FREE_FLOW" | "SLOW_MOVING" | "CONGESTED" | "INSUFFICIENT_DATA"
    color_hex: str             # Màu hiển thị HTML
    color_bgr: Tuple[int, int, int]
    vehicle_count: int         # Số xe hiện tại trong khung hình
    pcu_density: float         # Mật độ quy đổi PCU
    avg_speed_kmh: float       # Vận tốc trung bình (km/h)
    flow_rate_vpm: float       # Lưu lượng (xe/phút)
    queue_length_m: float      # Chiều dài hàng chờ ước lượng (m)
    avg_stop_time_s: float     # Thời gian dừng trung bình (s)
    tracked_vehicles: List[Dict[str, Any]]


class TrafficDatabaseLogger:
    """
    Tầng 6: Backend + Cơ sở dữ liệu SQLite lưu trạng thái, vị trí, thời gian và lịch sử ùn tắc.
    """

    def __init__(self, db_path: str = "traffic_congestion_history.db"):
        self.db_path = str(db_path)
        self.last_log_time = 0.0
        self.log_interval_sec = 3.0  # Ghi DB định kỳ mỗi 3 giây để không làm nghẽn luồng video
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS congestion_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        location_name TEXT,
                        status_label TEXT,
                        status_code TEXT,
                        vehicle_count INTEGER,
                        pcu_density REAL,
                        avg_speed_kmh REAL,
                        flow_rate_vpm REAL,
                        queue_length_m REAL,
                        avg_stop_time_s REAL
                    )
                """)
                conn.commit()
        except Exception as e:
            print(f"⚠️ Không thể khởi tạo SQLite DB ùn tắc: {e}")

    def log_report(self, location_name: str, report: TrafficCongestionReport, force: bool = False):
        now = time.time()
        if not force and (now - self.last_log_time) < self.log_interval_sec:
            return
        self.last_log_time = now
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO congestion_history (
                        location_name, status_label, status_code,
                        vehicle_count, pcu_density, avg_speed_kmh,
                        flow_rate_vpm, queue_length_m, avg_stop_time_s
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(location_name),
                    report.status_label,
                    report.status_code,
                    int(report.vehicle_count),
                    float(round(report.pcu_density, 2)),
                    float(round(report.avg_speed_kmh, 1)),
                    float(round(report.flow_rate_vpm, 1)),
                    float(round(report.queue_length_m, 1)),
                    float(round(report.avg_stop_time_s, 1)),
                ))
                conn.commit()
        except Exception as e:
            print(f"⚠️ Lỗi ghi lịch sử ùn tắc SQLite: {e}")


import unicodedata


def _strip_vn(text: str) -> str:
    """Chuyển chuỗi tiếng Việt có dấu thành không dấu để hiển thị gọn trên OpenCV."""
    if not text:
        return ""
    text = text.replace("đ", "d").replace("Đ", "D")
    norm = unicodedata.normalize("NFD", text)
    return "".join(c for c in norm if unicodedata.category(c) != "Mn")


def order_quad_points(pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """
    Sắp xếp 4 điểm bất kỳ theo thứ tự vòng khép kín quanh tâm (bắt đầu từ góc trên-trái)
    để đa giác 4 góc không bị bắt chéo cạnh.
    """
    if len(pts) != 4:
        return list(pts)
    cx = sum(p[0] for p in pts) / 4.0
    cy = sum(p[1] for p in pts) / 4.0
    sorted_pts = sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    # Xoay danh sách sao cho điểm có (x + y) nhỏ nhất (góc trên-trái) đứng đầu
    tl_idx = min(range(4), key=lambda i: sorted_pts[i][0] + sorted_pts[i][1])
    return sorted_pts[tl_idx:] + sorted_pts[:tl_idx]


def draw_traffic_roi_preview(
    frame_bgr: np.ndarray,
    roi_points_norm: Optional[List[Tuple[float, float]]] = None,
    draft_points: Optional[List[Tuple[int, int]]] = None,
    roi_label: str = "Lan duong"
) -> np.ndarray:
    """
    Vẽ khung 4 góc làn đường (hoặc các điểm đang chấm dở 1..3 góc) lên khung hình preview.
    """
    if frame_bgr is None:
        return np.zeros((720, 1280, 3), dtype=np.uint8)
    out = frame_bgr.copy()
    h, w = out.shape[:2]
    clean_lbl = _strip_vn(roi_label.strip()) if roi_label and roi_label.strip() else "LAN DUONG"

    # Trường hợp 1: Đang chấm dở (1, 2 hoặc 3 góc)
    if draft_points and 0 < len(draft_points) < 4:
        pts_px = [(int(p[0]), int(p[1])) for p in draft_points]
        for idx, pt in enumerate(pts_px):
            if idx > 0:
                cv2.line(out, pts_px[idx - 1], pt, (255, 210, 0), 1, cv2.LINE_AA)
            cv2.circle(out, pt, 5, (0, 215, 255), -1, cv2.LINE_AA)
            cv2.circle(out, pt, 6, (20, 25, 35), 1, cv2.LINE_AA)
            cv2.putText(
                out, f"Goc {idx + 1}", (pt[0] + 8, max(16, pt[1] - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 215, 255), 1, cv2.LINE_AA
            )
        return out

    # Trường hợp 2: Đã có đủ 4 góc
    if roi_points_norm and len(roi_points_norm) == 4:
        pts_px = [(int(round(rx * w)), int(round(ry * h))) for rx, ry in roi_points_norm]
        poly = np.array(pts_px, dtype=np.int32).reshape((-1, 1, 2))

        # Tô lớp phủ bán trong suốt nhẹ trong làn đường
        overlay = out.copy()
        cv2.fillPoly(overlay, [poly], (235, 180, 30))
        out = cv2.addWeighted(out, 0.86, overlay, 0.14, 0)

        # Vẽ viền 4 cạnh mảnh (thickness=1)
        cv2.polylines(out, [poly], True, (255, 215, 0), 1, cv2.LINE_AA)

        # Vẽ 4 nút góc để người dùng dễ nhìn và nhấp dời góc
        for idx, pt in enumerate(pts_px):
            cv2.circle(out, pt, 4, (0, 230, 255), -1, cv2.LINE_AA)
            cv2.circle(out, pt, 5, (20, 25, 35), 1, cv2.LINE_AA)

        # Vẽ nhãn tên làn đường tại góc đầu tiên
        lx, ly = pts_px[0]
        (tw, th), _ = cv2.getTextSize(clean_lbl, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
        by = max(th + 6, ly - 6)
        bx = max(4, min(w - tw - 10, lx))
        cv2.rectangle(out, (bx, by - th - 4), (bx + tw + 8, by + 3), (20, 25, 35), -1)
        cv2.rectangle(out, (bx, by - th - 4), (bx + tw + 8, by + 3), (255, 215, 0), 1, cv2.LINE_AA)
        cv2.putText(out, clean_lbl, (bx + 4, by - 1), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 230, 255), 1, cv2.LINE_AA)

    return out


class TrafficCongestionAnalyzer:
    """
    Hệ thống phân tích đặc trưng giao thông và xác định trạng thái ùn tắc đa chiều (Không gian - Thời gian).
    """

    def __init__(self, db_path: str = "traffic_congestion_history.db"):
        self.db_logger = TrafficDatabaseLogger(db_path=db_path)
        # Vùng làn đường 4 góc chuẩn hóa [(rx1, ry1), (rx2, ry2), (rx3, ry3), (rx4, ry4)]
        self.roi_points_norm: Optional[List[Tuple[float, float]]] = None
        self.roi_label: str = "Làn đường giám sát"
        self.reset()

    def set_roi_norm(self, points_norm: Optional[List[Tuple[float, float]]], label: Optional[str] = None):
        """Thiết lập hoặc cập nhật 4 góc vùng làn đường chuẩn hóa [0..1]."""
        if points_norm and len(points_norm) == 4:
            self.roi_points_norm = order_quad_points(points_norm)
        else:
            self.roi_points_norm = None
        if label is not None and str(label).strip():
            self.roi_label = str(label).strip()

    def clear_roi(self):
        """Xóa vùng làn đường 4 góc (quay về giám sát toàn khung hình)."""
        self.roi_points_norm = None

    def reset(self):
        """Đặt lại toàn bộ bộ nhớ theo dõi quỹ đạo khi đổi nguồn video / camera."""
        self.frame_count = 0
        self.start_sim_time: Optional[float] = None
        # Lưu lịch sử quỹ đạo cho từng track_id:
        # deque of (frame_idx, sim_time_s, smooth_cx, smooth_cy, bw, bh, speed_kmh)
        self.trajectories: Dict[int, deque] = {}
        # Lưu thời gian bắt đầu dừng (khi tốc độ < 3.5 km/h) của từng track_id
        self.stop_start_time: Dict[int, float] = {}
        # Lưu các track_id đã xuất hiện gần đây để tính lưu lượng (xe/phút)
        self.seen_ids_in_window: Dict[int, float] = {}
        # Làm mượt EMA cho tốc độ và mật độ
        self.ema_speed_kmh: Optional[float] = None
        self.ema_pcu: float = 0.0

    def _estimate_local_meter_per_pixel(
        self, cls_name: str, bw: float, bh: float, cy: float, frame_h: int, frame_w: int
    ) -> Tuple[float, float]:
        """
        Tự hiệu chỉnh tỷ lệ mét/pixel (Self-Calibrating Perspective Scale) tại vị trí thực của xe:
        1. Dựa trên kích thước thực của lớp phương tiện (VEHICLE_REAL_DIMS_M) chia cho kích thước BBox (bw, bh).
        2. Kết hợp mô hình phối cảnh mặt đường theo chiều cao `cy` để chống nhiễu khi xe bị che khuất một phần.
        3. Trả về (m_per_px_x, m_per_px_y) trong đó trục dọc `y` có hệ số foreshortening do góc nghiêng camera.
        """
        real_w_m, real_h_m = VEHICLE_REAL_DIMS_M.get(cls_name, (1.8, 1.5))

        # Tỷ lệ m/px suy từ BBox của chính xe đó
        bbox_scale_x = real_w_m / max(12.0, float(bw))
        bbox_scale_y = real_h_m / max(12.0, float(bh))
        bbox_m_per_px = 0.55 * bbox_scale_x + 0.45 * bbox_scale_y

        # Tỷ lệ m/px theo phối cảnh mặt đường chuẩn (chiều ngang lòng đường ~ 9m ở đáy ảnh)
        norm_y = max(0.18, min(1.0, float(cy) / max(1.0, float(frame_h))))
        road_m_per_px = (9.5 / max(320.0, float(frame_w))) / (norm_y ** 0.85)

        # Hòa trộn 65% kích thước thực của xe + 35% phối cảnh mặt đường, kẹp ngưỡng an toàn
        m_per_px_x = float(np.clip(0.65 * bbox_m_per_px + 0.35 * road_m_per_px, 0.004, 0.14))
        # Theo trục dọc màn hình (y), do góc nghiêng camera (camera pitch ~ 20-30 độ),
        # 1 pixel dọc trên mặt đường tương ứng quãng đường dài hơn ~ 1.45 - 2.1 lần ở phía xa
        foreshortening = float(np.clip(1.35 + 0.65 * (1.0 - norm_y), 1.25, 2.10))
        m_per_px_y = m_per_px_x * foreshortening
        return m_per_px_x, m_per_px_y

    def _compute_vehicle_speed_kmh(
        self,
        traj: deque,
        cls_name: str,
        smooth_cx: float,
        smooth_cy: float,
        bw: float,
        bh: float,
        sim_time_s: float,
        frame_h: int,
        frame_w: int,
    ) -> float:
        """
        Tính tốc độ thực (km/h) ổn định, chống nhiễu rung BBox (BBox Jitter Suppression):
        - So sánh vị trí hiện tại với vị trí cách đây ~ 0.25s - 0.60s trong lịch sử quỹ đạo.
        - Kiểm tra độ nhất quán hướng di chuyển (Direction Consistency Ratio) để loại bỏ rung lắc tại chỗ.
        - Nếu độ dịch chuyển nhỏ hơn ngưỡng tĩnh (deadband theo kích thước BBox), trả về 0.0 km/h.
        """
        if len(traj) < 2:
            return 0.0

        # Tìm điểm neo trong lịch sử cách thời điểm hiện tại khoảng 0.25s - 0.60s (hoặc điểm đầu tiên nếu mới có vài mẫu)
        anchor = traj[0]
        for item in reversed(traj):
            dt_cand = sim_time_s - item[1]
            if dt_cand >= 0.28:
                anchor = item
                break

        _, anchor_t, anchor_cx, anchor_cy, anchor_bw, anchor_bh, prev_spd = anchor
        last_prev_spd = traj[-1][6]
        dt = sim_time_s - anchor_t
        if dt < 0.025:
            return float(last_prev_spd)

        dx_px = smooth_cx - anchor_cx
        dy_px = smooth_cy - anchor_cy
        net_disp_px = math.hypot(dx_px, dy_px)

        avg_bw = 0.5 * (bw + anchor_bw)
        avg_bh = 0.5 * (bh + anchor_bh)
        veh_diag_px = math.hypot(avg_bw, avg_bh)

        # 1. Ngưỡng lọc đứng yên (Deadband):
        # Nếu trong cửa sổ thời gian, tâm xe dịch chuyển nhỏ hơn 5.5% kích thước xe (hoặc < 2.5px) -> Xe đang đứng yên!
        min_move_px = max(2.4, 0.055 * veh_diag_px)
        if net_disp_px < min_move_px:
            # Giảm nhanh về 0 nếu trước đó có tốc độ nhỏ
            return 0.0 if last_prev_spd < 4.5 else float(0.4 * last_prev_spd)

        # 2. Kiểm tra tính nhất quán hướng di chuyển (Path Linearity):
        # Nếu BBox chỉ rung qua lại tại chỗ, tổng quãng đường từng bước (path_len) sẽ lớn hơn nhiều so với net_disp_px
        pts = [(p[2], p[3]) for p in traj if p[1] >= anchor_t]
        pts.append((smooth_cx, smooth_cy))
        step_sum_px = 0.0
        for i in range(1, len(pts)):
            step_sum_px += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        linearity = net_disp_px / max(1e-3, step_sum_px)
        if linearity < 0.42:
            return 0.0 if last_prev_spd < 5.0 else float(0.5 * last_prev_spd)

        # 3. Quy đổi sang mét bằng tỷ lệ tự hiệu chỉnh tại vị trí xe
        mid_cy = 0.5 * (smooth_cy + anchor_cy)
        m_per_px_x, m_per_px_y = self._estimate_local_meter_per_pixel(
            cls_name, avg_bw, avg_bh, mid_cy, frame_h, frame_w
        )
        dist_m = math.hypot(dx_px * m_per_px_x, dy_px * m_per_px_y)
        raw_speed_kmh = (dist_m / dt) * 3.6

        # Kẹp trần hợp lý theo từng loại xe trong đô thị
        max_cap_kmh = 65.0 if cls_name in ("motorcycle", "motorbike", "bicycle") else 80.0
        raw_speed_kmh = float(np.clip(raw_speed_kmh, 0.0, max_cap_kmh))

        # Bộ lọc EMA mượt mà: nếu xe mới có tốc độ lần đầu thì nhận ngay 75% tốc độ thực
        if last_prev_spd <= 0.1:
            smoothed_speed = 0.75 * raw_speed_kmh
        else:
            smoothed_speed = 0.62 * last_prev_spd + 0.38 * raw_speed_kmh

        return 0.0 if smoothed_speed < 1.8 else float(smoothed_speed)

    @staticmethod
    def _enhance_frame_for_detection(frame_bgr: np.ndarray) -> np.ndarray:
        """Tăng cường tương phản cục bộ CLAHE nhẹ nếu khung hình camera bị tối, ngược sáng hoặc mưa mù."""
        lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB)
        l_chan, a_chan, b_chan = cv2.split(lab)
        mean_l = float(np.mean(l_chan))
        std_l = float(np.std(l_chan))
        if mean_l < 95.0 or std_l < 42.0:
            clahe = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8))
            l_chan = clahe.apply(l_chan)
            return cv2.cvtColor(cv2.merge((l_chan, a_chan, b_chan)), cv2.COLOR_LAB2BGR)
        return frame_bgr

    def analyze_frame(
        self,
        frame_bgr: np.ndarray,
        vehicle_model: Any,
        veh_conf: float = 0.30,
        fps: float = 25.0,
        is_single_image: bool = False,
        location_name: str = "Camera Giao Thông",
        sim_time_s: Optional[float] = None,
        return_bgr: bool = False,
        is_streaming: bool = False
    ) -> Tuple[np.ndarray, pd.DataFrame, str, TrafficCongestionReport]:
        """
        Thực thi trọn vẹn pipeline 7 bước trên 1 khung hình với tốc độ cao và ước lượng km/h chuẩn xác.
        """
        # Thu nhỏ khung hình trước nếu quá lớn (> 960px) để tăng tốc YOLO + ByteTrack và truyền WebSocket mượt mà
        orig_h, orig_w = frame_bgr.shape[:2]
        if orig_w > 960 and not is_single_image:
            scale = 960.0 / float(orig_w)
            work_frame = cv2.resize(frame_bgr, (960, int(orig_h * scale)), interpolation=cv2.INTER_LINEAR)
        else:
            work_frame = frame_bgr

        h, w = work_frame.shape[:2]
        self.frame_count += 1
        now_wall = time.time()
        cur_sim_t = float(sim_time_s) if sim_time_s is not None else now_wall
        if self.start_sim_time is None:
            self.start_sim_time = cur_sim_t

        tracked_list: List[Dict[str, Any]] = []

        # Quy đổi khung 4 góc chuẩn hóa sang tọa độ pixel trên work_frame (nếu người dùng đã vẽ vùng làn đường)
        roi_poly_px: Optional[np.ndarray] = None
        if self.roi_points_norm and len(self.roi_points_norm) == 4:
            pts_px = [(int(round(rx * w)), int(round(ry * h))) for rx, ry in self.roi_points_norm]
            roi_poly_px = np.array(pts_px, dtype=np.int32).reshape((-1, 1, 2))

        # 2. Phát hiện và theo dõi phương tiện bằng YOLOv8s + ByteTrack (imgsz=640 real-time, CLAHE + Bộ lọc Đa lớp Giao thông VN)
        if vehicle_model is not None:
            try:
                det_frame = self._enhance_frame_for_detection(work_frame)
                base_conf = max(0.12, float(veh_conf) * 0.65)

                # Tự động trích xuất danh sách class phù hợp với mô hình (custom 4 classes hoặc COCO 80 classes)
                target_classes = None
                if hasattr(vehicle_model, "names") and isinstance(vehicle_model.names, dict):
                    model_names = vehicle_model.names
                    if len(model_names) <= 5 and any(n in ("bus", "car", "motorbike", "truck") for n in model_names.values()):
                        target_classes = list(model_names.keys())
                    else:
                        valid_veh_names = {"car", "motorcycle", "motorbike", "bus", "truck", "bicycle"}
                        target_classes = [c_id for c_id, c_name in model_names.items() if str(c_name).lower() in valid_veh_names]

                use_half = bool(INFER_DEVICE != "cpu" and torch.cuda.is_available())
                with torch.inference_mode():
                    if is_single_image:
                        results = vehicle_model.predict(
                            det_frame,
                            conf=base_conf,
                            iou=0.48,
                            classes=target_classes,
                            imgsz=640 if not is_streaming else 480,
                            half=use_half,
                            device=INFER_DEVICE,
                            verbose=False
                        )[0]
                    else:
                        results = vehicle_model.track(
                            det_frame,
                            conf=base_conf,
                            iou=0.48,
                            classes=target_classes,
                            tracker="bytetrack.yaml",
                            persist=True,
                            imgsz=640 if not is_streaming else 480,
                            half=use_half,
                            device=INFER_DEVICE,
                            verbose=False
                        )[0]

                if results.boxes is not None and len(results.boxes) > 0:
                    xyxys = results.boxes.xyxy.cpu().numpy()
                    clss = results.boxes.cls.cpu().numpy().astype(int)
                    confs = results.boxes.conf.cpu().numpy()
                    if results.boxes.id is not None:
                        ids = results.boxes.id.cpu().numpy().astype(int)
                    else:
                        ids = np.arange(1, len(xyxys) + 1)

                    frame_area = float(w * h)
                    two_wheel_thresh = max(0.15, float(veh_conf) * 0.72)
                    four_wheel_thresh = float(veh_conf)

                    for box, cls_id, conf_val, tid in zip(xyxys, clss, confs, ids):
                        x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
                        bw = float(max(4, x2 - x1))
                        bh = float(max(4, y2 - y1))
                        raw_cx = 0.5 * (x1 + x2)
                        raw_cy = 0.5 * (y1 + y2)  # Dùng tâm hình học của BBox để ổn định hơn cạnh đáy khi bị che khuất
                        bottom_cy = float(y2) - 0.10 * bh  # Điểm bánh xe tiếp xúc mặt đường

                        cls_name = str(vehicle_model.names.get(int(cls_id), "car")).lower()

                        # Lọc ngưỡng đa lớp & hình học đặc thù giao thông đô thị Việt Nam
                        box_area = bw * bh
                        aspect = bw / max(1.0, bh)
                        if cls_name in ("motorcycle", "motorbike", "bicycle"):
                            if float(conf_val) < two_wheel_thresh:
                                continue
                            if bw < 8.0 or bh < 10.0 or box_area > 0.28 * frame_area or aspect > 3.2:
                                continue
                        else:
                            if float(conf_val) < four_wheel_thresh:
                                continue
                            if bw < 14.0 or bh < 14.0 or box_area > 0.55 * frame_area or aspect > 4.5 or aspect < 0.22:
                                continue

                        # Nếu đã thiết lập khung 4 góc làn đường: chỉ tính phương tiện đi vào bên trong khung 4 góc
                        if roi_poly_px is not None:
                            in_center = cv2.pointPolygonTest(roi_poly_px, (float(raw_cx), float(raw_cy)), False) >= 0
                            in_bottom = cv2.pointPolygonTest(roi_poly_px, (float(raw_cx), float(bottom_cy)), False) >= 0
                            if not (in_center or in_bottom):
                                continue

                        tid_int = int(tid)
                        self.seen_ids_in_window[tid_int] = cur_sim_t

                        if tid_int not in self.trajectories:
                            self.trajectories[tid_int] = deque(maxlen=20)

                        traj = self.trajectories[tid_int]

                        # Làm mượt tọa độ tâm BBox trước khi tính vận tốc để khử rung (Low-pass filter)
                        if len(traj) > 0:
                            prev_cx, prev_cy = traj[-1][2], traj[-1][3]
                            smooth_cx = 0.55 * raw_cx + 0.45 * prev_cx
                            smooth_cy = 0.55 * raw_cy + 0.45 * prev_cy
                        else:
                            smooth_cx, smooth_cy = raw_cx, raw_cy

                        # Tính tốc độ thực (km/h)
                        if is_single_image:
                            speed_kmh = 0.0
                        else:
                            speed_kmh = self._compute_vehicle_speed_kmh(
                                traj=traj,
                                cls_name=cls_name,
                                smooth_cx=smooth_cx,
                                smooth_cy=smooth_cy,
                                bw=bw,
                                bh=bh,
                                sim_time_s=cur_sim_t,
                                frame_h=h,
                                frame_w=w,
                            )

                        traj.append((self.frame_count, cur_sim_t, smooth_cx, smooth_cy, bw, bh, speed_kmh))

                        # Tính thời gian dừng của xe (khi tốc độ < 3.5 km/h và đã theo dõi >= 3 mẫu)
                        if not is_single_image and speed_kmh < 3.5 and len(traj) >= 3:
                            if tid_int not in self.stop_start_time:
                                self.stop_start_time[tid_int] = cur_sim_t
                            stop_dur_s = max(0.0, cur_sim_t - self.stop_start_time[tid_int])
                        else:
                            self.stop_start_time.pop(tid_int, None)
                            stop_dur_s = 0.0

                        tracked_list.append({
                            "id": tid_int,
                            "class": cls_name,
                            "viet_name": VEHICLE_VIET_NAMES.get(cls_name, "Xe"),
                            "bbox": (x1, y1, x2, y2),
                            "center": (int(smooth_cx), int(smooth_cy)),
                            "conf": float(conf_val),
                            "speed_kmh": float(speed_kmh),
                            "stop_time_s": float(stop_dur_s),
                            "pcu": VEHICLE_PCU_WEIGHTS.get(cls_name, 1.0),
                            "length_m": VEHICLE_AVG_LENGTH_M.get(cls_name, 4.0),
                            "trail": [(int(pt[2]), int(pt[3])) for pt in traj],
                        })
            except Exception as e:
                print(f"⚠️ Lỗi YOLO + ByteTrack trên khung hình giao thông: {e}")

        # Dọn dẹp các ID đã rời khỏi khung hình quá 60 giây
        stale_ids = [
            tid for tid, t_last in self.seen_ids_in_window.items()
            if (cur_sim_t - t_last) > 60.0 or (cur_sim_t - t_last) < -5.0
        ]
        for tid in stale_ids:
            self.seen_ids_in_window.pop(tid, None)
            self.trajectories.pop(tid, None)
            self.stop_start_time.pop(tid, None)

        # 3. Trích xuất 5 đặc trưng giao thông
        veh_count = len(tracked_list)
        total_pcu = sum(v["pcu"] for v in tracked_list)
        self.ema_pcu = 0.7 * self.ema_pcu + 0.3 * total_pcu if self.frame_count > 1 else total_pcu

        # Tốc độ trung bình dòng phương tiện (chỉ tính các xe đã có >= 2 mẫu quỹ đạo để không bị nhiễu 0km/h ở frame đầu)
        mature_vehicles = [v for v in tracked_list if len(v["trail"]) >= 2]
        if mature_vehicles:
            inst_speed = float(np.mean([v["speed_kmh"] for v in mature_vehicles]))
            self.ema_speed_kmh = inst_speed if self.ema_speed_kmh is None else (0.65 * self.ema_speed_kmh + 0.35 * inst_speed)
        elif veh_count == 0:
            self.ema_speed_kmh = 0.0

        avg_speed_kmh = float(self.ema_speed_kmh or 0.0)

        # Lưu lượng phương tiện (xe/phút)
        elapsed_min = max(0.15, (cur_sim_t - (self.start_sim_time or cur_sim_t)) / 60.0)
        unique_recent_count = len(self.seen_ids_in_window)
        flow_rate_vpm = unique_recent_count / elapsed_min if not is_single_image else float(veh_count * 10)

        # Chiều dài hàng chờ (m) và Thời gian dừng trung bình (s)
        slow_or_stopped = [
            v for v in mature_vehicles
            if v["speed_kmh"] < 7.0
        ] if not is_single_image else ([v for v in tracked_list] if veh_count >= 8 else [])

        queue_length_m = sum(v["length_m"] * 1.25 for v in slow_or_stopped) / 2.0
        stop_times = [v["stop_time_s"] for v in tracked_list if v["stop_time_s"] > 0.0]
        avg_stop_time_s = float(np.mean(stop_times)) if stop_times else 0.0

        # 4 & 5. Bộ xác định trạng thái ùn tắc (4 trạng thái: Thông thoáng / Lưu thông chậm / Ùn tắc / Chưa đủ dữ liệu)
        if not is_single_image and self.frame_count < 3:
            status_label = "Chưa đủ dữ liệu"
            status_code = "INSUFFICIENT_DATA"
            color_hex = "#94a3b8"
            color_bgr = (184, 163, 148)
        elif is_single_image:
            if veh_count == 0:
                status_label = "Thông thoáng"
                status_code = "FREE_FLOW"
                color_hex = "#22c55e"
                color_bgr = (94, 197, 34)
            elif total_pcu >= 12.0 or veh_count >= 16:
                status_label = "Ùn tắc"
                status_code = "CONGESTED"
                color_hex = "#ef4444"
                color_bgr = (68, 68, 239)
            elif total_pcu >= 5.5 or veh_count >= 7:
                status_label = "Lưu thông chậm"
                status_code = "SLOW_MOVING"
                color_hex = "#eab308"
                color_bgr = (8, 179, 234)
            else:
                status_label = "Thông thoáng"
                status_code = "FREE_FLOW"
                color_hex = "#22c55e"
                color_bgr = (94, 197, 34)
        else:
            if (
                (total_pcu >= 6.0 and avg_speed_kmh < 9.0)
                or queue_length_m >= 18.0
                or (avg_stop_time_s >= 10.0 and total_pcu >= 4.5)
                or total_pcu >= 15.0
            ):
                status_label = "Ùn tắc"
                status_code = "CONGESTED"
                color_hex = "#ef4444"
                color_bgr = (68, 68, 239)
            elif (
                (total_pcu >= 3.0 and avg_speed_kmh < 20.0)
                or queue_length_m >= 8.0
                or total_pcu >= 7.5
            ):
                status_label = "Lưu thông chậm"
                status_code = "SLOW_MOVING"
                color_hex = "#eab308"
                color_bgr = (8, 179, 234)
            else:
                status_label = "Thông thoáng"
                status_code = "FREE_FLOW"
                color_hex = "#22c55e"
                color_bgr = (94, 197, 34)

        report = TrafficCongestionReport(
            status_label=status_label,
            status_code=status_code,
            color_hex=color_hex,
            color_bgr=color_bgr,
            vehicle_count=veh_count,
            pcu_density=total_pcu,
            avg_speed_kmh=avg_speed_kmh,
            flow_rate_vpm=flow_rate_vpm,
            queue_length_m=queue_length_m,
            avg_stop_time_s=avg_stop_time_s,
            tracked_vehicles=tracked_list,
        )

        # 6. Lưu vào cơ sở dữ liệu SQLite
        self.db_logger.log_report(location_name, report, force=is_single_image)

        # 7. Vẽ HUD mảnh và tạo bảng thông số + cảnh báo
        hud_bgr = self.render_traffic_hud(work_frame, report, is_single_image=is_single_image)
        hud_rgb = hud_bgr if return_bgr else cv2.cvtColor(hud_bgr, cv2.COLOR_BGR2RGB)
        output_frame = hud_rgb

        spd_str = f"{avg_speed_kmh:.1f} km/h" if not is_single_image else "Ảnh tĩnh"
        df = pd.DataFrame([{
            "Trạng Thái": status_label,
            "Số Phương Tiện": f"{veh_count} xe ({total_pcu:.1f} PCU)",
            "Tốc Độ TB": spd_str,
            "Lưu Lượng": f"{flow_rate_vpm:.0f} xe/phút",
            "Hàng Chờ": f"{queue_length_m:.1f} m",
            "Thời Gian Dừng": f"{avg_stop_time_s:.1f} s",
        }])

        icon_map = {
            "FREE_FLOW": "🟢",
            "SLOW_MOVING": "🟡",
            "CONGESTED": "🔴",
            "INSUFFICIENT_DATA": "⚪",
        }
        icon = icon_map.get(status_code, "🚦")
        right_summary = (
            f"Số xe: <span style='color:#38bdf8;'>{veh_count} xe</span> | Tốc độ TB: <span style='color:#38bdf8;'>{spd_str}</span>"
            if not is_single_image
            else f"Số xe: <span style='color:#38bdf8;'>{veh_count} xe ({total_pcu:.1f} PCU)</span>"
        )

        alarm_html = f"""
        <div style="background:#1e293b; color:white; padding:10px 14px; border-radius:8px; border-left:5px solid {color_hex}; display:flex; justify-content:space-between; align-items:center;">
            <span style="color:{color_hex}; font-size:15px; font-weight:700;">{icon} Trạng thái: {status_label}</span>
            <span style="font-size:15px; font-weight:700;">{right_summary}</span>
        </div>
        """

        return hud_rgb, df, alarm_html, report

    def render_traffic_hud(
        self,
        frame_bgr: np.ndarray,
        report: TrafficCongestionReport,
        is_single_image: bool = False
    ) -> np.ndarray:
        """
        Vẽ khung 4 góc làn đường (nếu có), BBox phương tiện nét mảnh (thickness=1),
        quỹ đạo di chuyển (trajectory tail) và nhãn tối giản (#ID | km/h).
        """
        out = frame_bgr.copy()
        h, w = out.shape[:2]

        # Vẽ khung 4 góc làn đường (nếu người dùng đã thiết lập)
        if self.roi_points_norm and len(self.roi_points_norm) == 4:
            pts_px = [(int(round(rx * w)), int(round(ry * h))) for rx, ry in self.roi_points_norm]
            poly = np.array(pts_px, dtype=np.int32).reshape((-1, 1, 2))
            overlay = out.copy()
            cv2.fillPoly(overlay, [poly], report.color_bgr)
            out = cv2.addWeighted(out, 0.90, overlay, 0.10, 0)
            cv2.polylines(out, [poly], True, (255, 215, 0), 1, cv2.LINE_AA)
            for pt in pts_px:
                cv2.circle(out, pt, 3, (0, 230, 255), -1, cv2.LINE_AA)
            clean_lbl = _strip_vn(self.roi_label.strip()) if self.roi_label and self.roi_label.strip() else "LAN DUONG"
            lx, ly = pts_px[0]
            (tw, th), _ = cv2.getTextSize(clean_lbl, cv2.FONT_HERSHEY_SIMPLEX, 0.40, 1)
            by = max(th + 5, ly - 5)
            bx = max(4, min(w - tw - 10, lx))
            cv2.rectangle(out, (bx, by - th - 3), (bx + tw + 6, by + 2), (20, 25, 35), -1)
            cv2.putText(out, clean_lbl, (bx + 3, by - 1), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 230, 255), 1, cv2.LINE_AA)

        for v in report.tracked_vehicles:
            x1, y1, x2, y2 = v["bbox"]
            spd = v["speed_kmh"]
            tid = v["id"]
            v_name = v["viet_name"]
            trail = v["trail"]

            # Màu theo vận tốc từng xe: Đỏ nếu dừng/bò chậm (<5 km/h), Vàng nếu chậm (5-18 km/h), Xanh nếu lưu thông tốt
            if not is_single_image and len(trail) >= 2:
                if spd < 5.0:
                    box_color = (60, 60, 255)    # Đỏ
                elif spd < 18.0:
                    box_color = (0, 200, 255)    # Vàng
                else:
                    box_color = (80, 230, 50)    # Xanh lá
            else:
                box_color = (255, 200, 0)        # Xanh cyan nhạt

            # Vẽ quỹ đạo di chuyển mảnh (thickness=1)
            if len(trail) >= 2:
                pts = np.array(trail, dtype=np.int32).reshape((-1, 1, 2))
                cv2.polylines(out, [pts], False, box_color, 1, cv2.LINE_AA)
                cv2.circle(out, trail[-1], 2, box_color, -1, cv2.LINE_AA)

            # Vẽ BBox mảnh (thickness=1)
            cv2.rectangle(out, (x1, y1), (x2, y2), box_color, 1, cv2.LINE_AA)

            # Nhãn tối giản phía trên xe
            if is_single_image:
                lbl = f"{v_name}"
            elif len(trail) < 2:
                lbl = f"#{tid}"
            else:
                lbl = f"#{tid} {spd:.0f}km/h"

            (tw, th), _ = cv2.getTextSize(lbl, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
            ly = max(th + 4, y1 - 3)
            cv2.rectangle(out, (x1, ly - th - 3), (x1 + tw + 4, ly + 2), (20, 25, 35), -1)
            cv2.putText(
                out, lbl, (x1 + 2, ly - 1),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, box_color, 1, cv2.LINE_AA
            )

        return out
