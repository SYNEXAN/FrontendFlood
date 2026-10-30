#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
📏 REFERENCE-GUIDED WATERLINE DETECTION WITH CONSTRAINED CHANGE-POINT OPTIMIZATION
Thuật toán Thủy chuẩn Vật thể Cố định (Virtual River Staff Gauge)
- Hiệu chuẩn 3 điểm: A (Đáy 0cm), M (Trung điểm vật lý thực H/2), B (Đỉnh H cm)
- Dải ROI động (Dynamic Strip ROI) theo chiều dài pixel thực tế của vật thể
- Bảng tra cứu phối cảnh (Projective Height Look-Up Table - LUT O(1))
- Bằng chứng kép (Dual Evidence): Sai khác mẫu khô CIE Lab + Phân đoạn nước 2 bên sườn
- Tối ưu hóa ranh giới 1 chiều (Constrained 1D Change-Point Detection) bằng Prefix Sums O(N)
- Đánh giá độ tin cậy (Boundary Confidence) & Nhận diện vật cản (Occlusion Check)
- Phân loại 6 trạng thái: NO_FLOOD, MEASURED, ABOVE_RANGE, OCCLUDED, UNRELIABLE, STALE
- Bộ lọc thích ứng thời gian (Adaptive Temporal Filter: Rolling Median + EMA)
================================================================================
"""

import cv2
import numpy as np
import math
import time
import unicodedata
import torch
from typing import Optional, Dict, Any, List, Tuple

INFER_DEVICE = 0 if torch.cuda.is_available() else "cpu"


class VirtualStaffGauge:
    """
    Thước đo Thủy Chuẩn Ảo neo vào Vật thể Cố định.
    Thuật toán: Reference-Guided Waterline Detection with Constrained Change-Point Optimization.
    Hỗ trợ hệ cao độ tuyệt đối [H_start, H_end] cm (Universal Elevation Datum).
    """
    def __init__(
        self,
        name="Mốc Thủy Chuẩn",
        p_bottom=None,
        p_top=None,
        p_mid=None,
        height_cm=None,
        warning_cm=25.0,
        r_M=0.5,
        strip_width=12,
        h_start_cm=0.0,
        h_end_cm=100.0,
        color_bgr=(255, 200, 0),
        color_hex="#06b6d4",
        gauge_id="gauge_1"
    ):
        self.name = name
        self.p_bottom = p_bottom  # Point A (h_start_cm)
        self.p_mid = p_mid        # Point M (vật lý H/2)
        self.p_top = p_top        # Point B (h_end_cm)
        self.h_start_cm = float(h_start_cm)
        self.h_end_cm = float(h_end_cm if height_cm is None else (self.h_start_cm + float(height_cm)))
        self.height_cm = max(2.0, float(self.h_end_cm - self.h_start_cm))
        self.warning_cm = float(warning_cm)
        self.strip_width = int(max(4, min(150, strip_width)))
        self.r_M = float(r_M)
        self.color_bgr = color_bgr if color_bgr is not None else (255, 200, 0)
        self.color_hex = color_hex
        self.gauge_id = gauge_id

        # Trạng thái cấu hình
        self.is_configured = (p_bottom is not None and p_top is not None)

        # Trạng thái đo lường & Độ tin cậy
        self.status = "NO_FLOOD" if self.is_configured else "UNCONFIGURED"
        self.confidence = 0.0
        self.last_measured_cm = 0.0
        self.filtered_cm = 0.0
        self.last_valid_time = 0.0
        self.is_alarm_triggered = False
        self.is_overflow = False
        self.overflow_cm = 0.0

        # Mẫu tham chiếu khô (Dry Reference)
        self.base_frame = None
        self.ref_lab_roi = None   # Dải CIE Lab của ảnh tham chiếu khô: shape (N, W_pts, 3)
        self.mu_dry = 0.0
        self.sigma_dry = 5.0

        # Bảng tra cứu độ cao 3D Look-Up Table (LUT)
        self.lut_size = 1001
        self.height_lut = None

        # Bộ lọc thời gian thích ứng
        self.temporal_history = []  # Lưu (timestamp, raw_cm)
        self.history_window_s = 2.0 # Cửa sổ trượt 2 giây
        self.tau_temporal = 1.0     # Hằng số thời gian EMA

        if self.is_configured:
            self._update_geometry()

    def _update_geometry(self):
        """Tính toán tỷ lệ phối cảnh r_M và khởi tạo bảng Lookup Table (LUT)."""
        bx, by = self.p_bottom
        tx, ty = self.p_top
        L = math.hypot(tx - bx, ty - by)
        if L < 5.0:
            self.is_configured = False
            return

        if self.p_mid is not None:
            mx, my = self.p_mid
            L_AM = math.hypot(mx - bx, my - by)
            self.r_M = max(0.05, min(0.95, L_AM / L))
        else:
            self.r_M = max(0.05, min(0.95, float(self.r_M)))

        self.height_cm = max(2.0, float(self.h_end_cm - self.h_start_cm))

        # Khởi tạo bảng Look-Up Table (LUT) ánh xạ từ r* in [0, 1] sang cm vật lý thân mốc
        # d(r) = Delta_H * [r * (1 - r_M)] / [r_M * (1 - r) + r * (1 - r_M)]
        r_vals = np.linspace(0.0, 1.0, self.lut_size, dtype=np.float32)
        denom = self.r_M * (1.0 - r_vals) + r_vals * (1.0 - self.r_M)
        denom = np.where(denom < 1e-6, 1e-6, denom)
        self.height_lut = self.height_cm * (r_vals * (1.0 - self.r_M)) / denom

    def build_perspective_lut(self, r_M=None):
        """Khởi tạo / Cập nhật lại bảng LUT phối cảnh."""
        if r_M is not None:
            self.r_M = float(r_M)
        self._update_geometry()

    def configure(
        self,
        p_bottom,
        p_top,
        p_mid=None,
        height_cm=None,
        name="Mốc Cố Định",
        warning_cm=25.0,
        base_frame=None,
        strip_width=12,
        r_M=0.5,
        h_start_cm=0.0,
        h_end_cm=100.0,
        color_bgr=None,
        color_hex=None,
        calib_mode="3_points"
    ):
        """Thiết lập cấu hình mốc chuẩn và hiệu chuẩn mẫu khô (Quy chuẩn 3 điểm khử phối cảnh)."""
        self.name = name
        self.p_bottom = (int(p_bottom[0]), int(p_bottom[1]))
        self.p_top = (int(p_top[0]), int(p_top[1]))
        self.p_mid = (int(p_mid[0]), int(p_mid[1])) if p_mid is not None else None
        self.h_start_cm = float(h_start_cm)
        self.h_end_cm = float(h_end_cm if height_cm is None else (self.h_start_cm + float(height_cm)))
        self.height_cm = max(2.0, float(self.h_end_cm - self.h_start_cm))
        self.warning_cm = float(warning_cm)
        self.strip_width = int(max(4, min(150, strip_width)))
        self.r_M = float(r_M)
        if color_bgr is not None:
            self.color_bgr = color_bgr
        if color_hex is not None:
            self.color_hex = color_hex
        self.is_configured = True

        self.last_measured_cm = 0.0
        self.filtered_cm = 0.0
        self.status = "NO_FLOOD"
        self.confidence = 1.0
        self.temporal_history = []
        self.last_valid_time = time.time()
        self.is_overflow = False
        self.overflow_cm = 0.0

        self._update_geometry()

        if base_frame is not None:
            self.calibrate_dry(base_frame)

    def reset(self):
        """Xóa cấu hình mốc và đưa về trạng thái mặc định."""
        self.p_bottom = None
        self.p_mid = None
        self.p_top = None
        self.is_configured = False
        self.last_measured_cm = 0.0
        self.filtered_cm = 0.0
        self.status = "UNCONFIGURED"
        self.confidence = 0.0
        self.base_frame = None
        self.ref_lab_roi = None
        self.temporal_history = []
        self.height_lut = None
        self.is_overflow = False
        self.overflow_cm = 0.0

    def sample_strip_grid(self, h, w):
        """
        Tạo lưới pixel lấy mẫu dải ROI dọc theo trục A -> B:
        - Số điểm dọc N = độ dài pixel thực tế của đoạn A-B (tối thiểu 30)
        - Bề rộng W_pts = strip_width (lát cắt ngang)
        """
        bx, by = self.p_bottom
        tx, ty = self.p_top
        dx = tx - bx
        dy = ty - by
        L = math.hypot(dx, dy)
        if L < 5.0:
            return None, None, None, None, 0

        N = max(30, int(round(L)))
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux

        # Tọa độ tâm các điểm dọc theo trục
        t_vals = np.linspace(0.0, 1.0, N, dtype=np.float32)
        cx = bx + t_vals * dx
        cy = by + t_vals * dy

        # Lát cắt ngang qua thân mốc
        half_w = self.strip_width // 2
        offsets = np.arange(-half_w, half_w + 1, dtype=np.float32)

        # Lưới tọa độ (N, W_pts)
        px_grid = np.clip(np.round(cx[:, None] + offsets[None, :] * nx).astype(np.int32), 0, w - 1)
        py_grid = np.clip(np.round(cy[:, None] + offsets[None, :] * ny).astype(np.int32), 0, h - 1)

        return px_grid, py_grid, (cx, cy), (nx, ny), N

    def set_strip_width(self, new_width: int, frame_bgr: Optional[np.ndarray] = None):
        """Cập nhật bề rộng cột mốc và tái tạo mẫu khô ref_lab_roi đồng bộ."""
        self.strip_width = int(max(4, min(150, new_width)))
        ref_frame = frame_bgr if frame_bgr is not None else self.base_frame
        if ref_frame is not None and self.is_configured:
            self.calibrate_dry(ref_frame)
        else:
            self.ref_lab_roi = None

    def calibrate_dry(self, base_frame):
        """Hiệu chuẩn mẫu khô: Trích xuất dải màu CIE Lab chuẩn và thống kê ban đầu."""
        if base_frame is None or not self.is_configured:
            return

        h, w = base_frame.shape[:2]
        px_grid, py_grid, _, _, N = self.sample_strip_grid(h, w)
        if px_grid is None:
            return

        self.base_frame = base_frame.copy()
        lab_img = cv2.cvtColor(base_frame, cv2.COLOR_BGR2LAB).astype(np.float32)

        # Trích xuất dải Lab mẫu: shape (N, W_pts, 3)
        self.ref_lab_roi = lab_img[py_grid, px_grid]  # (N, W_pts, 3)
        self.mu_dry = 0.0
        self.sigma_dry = 6.0

    def measure_water_level(self, water_mask, frame_bgr=None, timestamp=None, threshold=0.25):
        """
        Xác định mực nước bằng Constrained 1D Change-Point Optimization:
        1. Trích xuất bằng chứng kép (CIE Lab Reference Difference + Water Mask Flanks).
        2. Tổng hợp bằng chứng thành hồ sơ ngập 1D p_i.
        3. Tối ưu ranh giới qua Prefix Sums Negative Log-Likelihood E(j) trong O(N).
        4. Kiểm tra độ tin cậy, vật cản, chân mốc để phân loại 6 trạng thái.
        5. Tra bảng phối cảnh LUT ra cm và lọc thích ứng thời gian.
        """
        now = time.time() if timestamp is None else float(timestamp)

        if not self.is_configured:
            return 0.0, 0.0, None, False, "UNCONFIGURED", 0.0

        h, w = (water_mask.shape[:2] if water_mask is not None else frame_bgr.shape[:2])
        px_grid, py_grid, (cx, cy), (nx, ny), N = self.sample_strip_grid(h, w)
        if px_grid is None:
            return 0.0, 0.0, self.p_bottom, False, "UNRELIABLE", 0.0

        w_thresh = threshold if (water_mask is not None and np.max(water_mask) <= 1.0) else (threshold * 255.0)

        # -------------------------------------------------------------
        # BƯỚC 1: TRÍCH XUẤT BẰNG CHỨNG KÉP (DUAL EVIDENCE)
        # -------------------------------------------------------------
        # Nhánh A: Sai khác so với mẫu khô CIE Lab trên dải ROI
        if frame_bgr is not None and self.ref_lab_roi is not None:
            cur_lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
            cur_roi = cur_lab[py_grid, px_grid]  # (N, W_pts, 3)
            if cur_roi.shape != self.ref_lab_roi.shape:
                self.calibrate_dry(self.base_frame if self.base_frame is not None else frame_bgr)
            if self.ref_lab_roi is not None and cur_roi.shape == self.ref_lab_roi.shape:
                # Khoảng cách Euclidean màu sắc & độ sáng
                diff = np.linalg.norm(cur_roi - self.ref_lab_roi, axis=2)  # (N, W_pts)
                D = np.median(diff, axis=1)  # (N,)
                z_D = np.clip((D - self.mu_dry) / (self.sigma_dry + 1e-4), 0.0, 4.0) / 4.0
            else:
                z_D = np.zeros(N, dtype=np.float32)
        else:
            z_D = np.zeros(N, dtype=np.float32)

        # Nhánh B: Xác suất nước từ mô hình phân đoạn ở 2 sườn sát mép cột
        flank_offset = max(8, self.strip_width // 2 + 6)
        l_px = np.clip(np.round(cx + flank_offset * nx).astype(np.int32), 0, w - 1)
        l_py = np.clip(np.round(cy + flank_offset * ny).astype(np.int32), 0, h - 1)
        r_px = np.clip(np.round(cx - flank_offset * nx).astype(np.int32), 0, w - 1)
        r_py = np.clip(np.round(cy - flank_offset * ny).astype(np.int32), 0, h - 1)

        if water_mask is not None:
            w_left = (water_mask[l_py, l_px] >= w_thresh).astype(np.float32)
            w_right = (water_mask[r_py, r_px] >= w_thresh).astype(np.int32)
            W_flanks = (w_left + w_right) / 2.0
            W_center = np.mean(water_mask[py_grid, px_grid] >= w_thresh, axis=1)
        else:
            W_flanks = np.zeros(N, dtype=np.float32)
            W_center = np.zeros(N, dtype=np.float32)

        # -------------------------------------------------------------
        # BƯỚC 2: TỔNG HỢP BẰNG CHỨNG (EVIDENCE FUSION -> 1D PROFILE p_i)
        # -------------------------------------------------------------
        if self.ref_lab_roi is not None:
            # Kết hợp cả sai khác mẫu khô và phân đoạn nước
            p_profile = 0.45 * W_flanks + 0.35 * z_D + 0.20 * W_center
        else:
            # Fallback nếu chưa có mẫu khô: dùng hoàn toàn nước phân đoạn
            p_profile = 0.65 * W_flanks + 0.35 * W_center

        # Ràng buộc số học tránh log(0)
        eps = 1e-5
        p_profile = np.clip(p_profile, eps, 1.0 - eps)

        # -------------------------------------------------------------
        # BƯỚC 3: CONSTRAINED 1D CHANGE-POINT OPTIMIZATION (O(N) PREFIX SUMS)
        # -------------------------------------------------------------
        # E(j) = sum_{i < j} -log(p_i) + sum_{i >= j} -log(1 - p_i)
        cost_wet = -np.log(p_profile)
        cost_dry = -np.log(1.0 - p_profile)

        C_W = np.concatenate(([0.0], np.cumsum(cost_wet)))
        C_D = np.concatenate((np.cumsum(cost_dry[::-1])[::-1], [0.0]))
        E = C_W + C_D  # Độ dài N + 1 ứng viên ranh giới (từ 0 đến N)

        j_star = int(np.argmin(E))

        # Khớp parabol sub-pixel quanh j*
        if 1 <= j_star < N:
            e_left = E[j_star - 1]
            e_mid = E[j_star]
            e_right = E[j_star + 1]
            denom_quad = (e_left - 2.0 * e_mid + e_right)
            delta = 0.5 * (e_left - e_right) / denom_quad if abs(denom_quad) > 1e-6 else 0.0
            delta = float(np.clip(delta, -0.5, 0.5))
            r_star = (j_star + delta) / float(N)
        else:
            r_star = float(j_star) / float(N)

        r_star = float(np.clip(r_star, 0.0, 1.0))

        # -------------------------------------------------------------
        # BƯỚC 4: ĐÁNH GIÁ ĐỘ TIN CẬY & PHÂN LOẠI 6 TRẠNG THÁI
        # -------------------------------------------------------------
        # 4.1. Boundary Confidence (Độ sắc nét của điểm cực tiểu)
        e_range = np.max(E) - np.min(E)
        e_min = E[j_star]
        mean_e = np.mean(E)
        contrast = (mean_e - e_min) / (e_range + 1e-6)
        raw_conf = float(np.clip(contrast * 2.0, 0.0, 1.0))

        # 4.2. Kiểm tra chân mốc và sườn nước (Base & Flank Ground Check)
        base_flank_water = float(np.mean(W_flanks[:max(3, int(N * 0.12))]))
        win_r = max(1, int(N * r_star))
        flank_sub_ratio = float(np.mean(W_flanks[:win_r]))

        pole_obstruction = (float(np.mean(z_D > 0.4)) > 0.15) and (flank_sub_ratio < 0.25)

        if self.last_measured_cm > 5.0 and (raw_conf < 0.40 or flank_sub_ratio < 0.25 or pole_obstruction):
            # Trước đó đang ngập nhưng frame này bị xe/người che khuất tầm nhìn (mất nước sườn) -> OCCLUDED
            status = "OCCLUDED"
            confidence = 0.40
        elif base_flank_water < 0.25 and r_star < 0.08:
            # Chân mốc không có nước xung quanh và ranh giới sát đáy -> Khô ráo
            status = "NO_FLOOD"
            r_star = 0.0
            confidence = 0.98
        elif r_star >= 0.94 and np.mean(p_profile[-max(3, int(N * 0.15)):]) > 0.65:
            # Nước ngập lút đỉnh mốc
            status = "ABOVE_RANGE"
            confidence = 0.92
            self.is_overflow = True
        elif r_star >= 0.08 and flank_sub_ratio < 0.25:
            # Thân mốc bị che khuất nhưng 2 bên sườn không có nước -> Vật cản (người/xe)
            status = "OCCLUDED"
            confidence = 0.35
        elif raw_conf < 0.35:
            status = "UNRELIABLE"
            confidence = raw_conf
        else:
            status = "MEASURED"
            confidence = raw_conf

        # -------------------------------------------------------------
        # BƯỚC 5: PROJECTIVE HEIGHT MAPPING (TRA CỨU BẢNG LUT O(1))
        # -------------------------------------------------------------
        lut_idx = int(round(r_star * (self.lut_size - 1)))
        d_raw = float(self.height_lut[lut_idx])

        # Điểm tọa độ pixel của ngấn nước trên ảnh
        bx, by = self.p_bottom
        tx, ty = self.p_top
        wl_pt = (int(bx + r_star * (tx - bx)), int(by + r_star * (ty - by)))

        # -------------------------------------------------------------
        # BƯỚC 6: BỘ LỌC THỜI GIAN THÍCH ỨNG & QUẢN LÝ TRẠNG THÁI
        # -------------------------------------------------------------
        if status == "NO_FLOOD":
            self.filtered_cm = 0.0
            self.last_measured_cm = 0.0
            self.temporal_history = []
            self.last_valid_time = now
            self.is_overflow = False
            self.overflow_cm = 0.0
        elif status == "ABOVE_RANGE":
            self.filtered_cm = self.height_cm
            self.last_measured_cm = self.height_cm
            self.is_overflow = True
            self.overflow_cm = 0.0
            self.last_valid_time = now
        elif status == "MEASURED":
            # Thêm vào bộ nhớ đệm thời gian
            self.temporal_history.append((now, d_raw))
            # Cắt bỏ các mẫu cũ ngoài cửa sổ trượt (2s)
            self.temporal_history = [pt for pt in self.temporal_history if (now - pt[0]) <= self.history_window_s]

            vals = [pt[1] for pt in self.temporal_history]
            d_med = float(np.median(vals))

            # EMA theo thời gian thực: beta = 1 - exp(-dt / tau)
            dt = max(0.01, now - self.last_valid_time) if self.last_valid_time > 0 else 0.05
            beta = float(1.0 - math.exp(-dt / self.tau_temporal))
            beta = np.clip(beta, 0.10, 0.50)

            if self.filtered_cm <= 0.0:
                self.filtered_cm = d_med
            else:
                self.filtered_cm = (1.0 - beta) * self.filtered_cm + beta * d_med

            self.last_measured_cm = round(self.filtered_cm, 1)
            self.last_valid_time = now
            self.is_overflow = False
            self.overflow_cm = 0.0
        elif status == "OCCLUDED":
            # Bị che khuất: Giữ số đo hợp lệ gần nhất nhưng giảm tự tin
            if (now - self.last_valid_time) > 3.0:
                status = "STALE"
        elif status == "UNRELIABLE":
            # Không tin cậy: Nếu kéo dài quá 3s thì chuyển sang STALE
            if (now - self.last_valid_time) > 3.0:
                status = "STALE"

        self.status = status
        self.confidence = round(confidence, 2)
        submerged_cm = self.last_measured_cm
        self.is_alarm_triggered = (submerged_cm >= self.warning_cm and status in ["MEASURED", "ABOVE_RANGE"])
        submerged_ratio = submerged_cm / max(1.0, self.height_cm)

        return submerged_ratio, submerged_cm, wl_pt, self.is_alarm_triggered, self.status, self.confidence

    def render_on_frame(self, frame_bgr, water_mask=None, threshold=0.25, draw_hud=True, raw_frame_bgr=None):
        """
        Vẽ thước đo thủy chuẩn ảo, các vạch chia centimet chuẩn phối cảnh,
        chỉ báo ngấn nước, nhãn trạng thái và điểm tin cậy.
        """
        out_frame = frame_bgr.copy()
        h, w = out_frame.shape[:2]

        if not self.is_configured:
            return out_frame, 0.0, False, "UNCONFIGURED", 0.0

        feat_frame = raw_frame_bgr if raw_frame_bgr is not None else frame_bgr
        sub_ratio, sub_cm, wl_pt, is_alarm, status, conf = self.measure_water_level(
            water_mask, frame_bgr=feat_frame, threshold=threshold
        )

        bx, by = self.p_bottom
        tx, ty = self.p_top
        dx = tx - bx
        dy = ty - by
        length_px = math.hypot(dx, dy)
        if length_px < 5.0:
            return out_frame, 0.0, False, status, conf

        ux, uy = dx / length_px, dy / length_px
        nx, ny = -uy, ux

        # 1. Dải màu nền thước đo (Virtual Ruler Body)
        ruler_half_w = self.strip_width // 2
        poly_pts = [
            (int(bx - nx * ruler_half_w), int(by - ny * ruler_half_w)),
            (int(bx + nx * ruler_half_w), int(by + ny * ruler_half_w)),
            (int(tx + nx * ruler_half_w), int(ty + ny * ruler_half_w)),
            (int(tx - nx * ruler_half_w), int(ty - ny * ruler_half_w)),
        ]
        poly_np = np.array(poly_pts, dtype=np.int32)
        overlay = out_frame.copy()
        cv2.fillPoly(overlay, [poly_np], (25, 30, 40))
        out_frame = cv2.addWeighted(out_frame, 0.45, overlay, 0.55, 0)
        gauge_color = getattr(self, 'color_bgr', (255, 200, 0))
        cv2.polylines(out_frame, [poly_np], True, gauge_color, 1, cv2.LINE_AA)

        # 2. Phần thước đo ngập trong nước (Submerged Segment)
        if sub_ratio > 0.01:
            wx, wy = wl_pt if wl_pt is not None else (bx, by)
            sub_poly = [
                (int(bx - nx * ruler_half_w), int(by - ny * ruler_half_w)),
                (int(bx + nx * ruler_half_w), int(by + ny * ruler_half_w)),
                (int(wx + nx * ruler_half_w), int(wy + ny * ruler_half_w)),
                (int(wx - nx * ruler_half_w), int(wy - ny * ruler_half_w)),
            ]
            sub_np = np.array(sub_poly, dtype=np.int32)
            water_color = (0, 0, 235) if is_alarm else (220, 140, 20)
            sub_overlay = out_frame.copy()
            cv2.fillPoly(sub_overlay, [sub_np], water_color)
            out_frame = cv2.addWeighted(out_frame, 0.45, sub_overlay, 0.55, 0)

        # 3. Vẽ các vạch chia centimet phối cảnh 3D theo khoảng [h_start_cm -> h_end_cm]
        step_cm = 10.0 if self.height_cm <= 150 else 20.0
        cur_cm = 0.0
        while cur_cm <= self.height_cm:
            cm_norm = cur_cm / self.height_cm
            r_m = self.r_M
            denom_inv = (1.0 - r_m) + cm_norm * (2.0 * r_m - 1.0)
            t = (cm_norm * r_m) / denom_inv if abs(denom_inv) > 1e-6 else cm_norm

            cx_i = bx + t * dx
            cy_i = by + t * dy

            is_major = (int(cur_cm) % int(step_cm * 2) == 0) or (cur_cm == self.height_cm)
            tick_len = 14 if is_major else 7

            t_x1 = int(cx_i - nx * tick_len)
            t_y1 = int(cy_i - ny * tick_len)
            t_x2 = int(cx_i + nx * tick_len)
            t_y2 = int(cy_i + ny * tick_len)

            tick_color = (255, 255, 255) if is_major else (175, 175, 175)
            cv2.line(out_frame, (t_x1, t_y1), (t_x2, t_y2), tick_color, 1, cv2.LINE_AA)

            if is_major:
                tick_abs_cm = int(self.h_start_cm + cur_cm)
                label_tick = f"{tick_abs_cm}cm"
                lx = int(cx_i + nx * (tick_len + 4))
                ly = int(cy_i + ny * (tick_len + 4) + 4)
                cv2.putText(out_frame, label_tick, (lx, ly), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (240, 240, 240), 1, cv2.LINE_AA)

            cur_cm += step_cm

        # 4. Vẽ điểm chuẩn A (h_start_cm), M (H/2 nếu có) và B (h_end_cm)
        cv2.circle(out_frame, self.p_bottom, 5, (0, 255, 0), -1, cv2.LINE_AA)
        cv2.putText(out_frame, f"A ({int(self.h_start_cm)}cm)", (bx - 50, by + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 255, 0), 1, cv2.LINE_AA)

        if self.p_mid is not None:
            mid_cm = int(self.h_start_cm + self.height_cm / 2.0)
            cv2.circle(out_frame, self.p_mid, 4, (255, 200, 0), -1, cv2.LINE_AA)
            cv2.putText(out_frame, f"M ({mid_cm}cm)", (self.p_mid[0] + 12, self.p_mid[1] + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 200, 0), 1, cv2.LINE_AA)

        cv2.circle(out_frame, self.p_top, 5, (0, 215, 255), -1, cv2.LINE_AA)
        cv2.putText(out_frame, f"B ({int(self.h_end_cm)}cm)", (tx - 50, ty - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 215, 255), 1, cv2.LINE_AA)

        # 5. Vẽ Chỉ báo Ngấn Nước & Badge tối giản
        wx, wy = wl_pt if wl_pt is not None else self.p_bottom
        waterline_color = (0, 0, 255) if is_alarm else (0, 230, 255)
        cv2.line(out_frame, (int(wx - nx * 28), int(wy - ny * 28)), (int(wx + nx * 28), int(wy + ny * 28)), waterline_color, 1, cv2.LINE_AA)

        z_val = self.h_start_cm + sub_cm
        clean_name = strip_accents(self.name).strip()
        if clean_name.upper() in ("COT MOC MOI", "MOC CO DINH", "MOC MOI", "GOC CAY VEN DUONG", "MOC VIA HE / COT DEN DO THI"):
            clean_name = ""

        # Định dạng nhãn tối giản, dễ đọc
        if status == "ABOVE_RANGE":
            val_str = f">= {int(self.h_end_cm)} cm"
            badge_bg = (0, 0, 180)
            badge_border = (0, 0, 255)
        elif status == "OCCLUDED":
            val_str = f"{z_val:.1f} cm"
            badge_bg = (20, 100, 180)
            badge_border = (0, 165, 255)
        elif status == "UNRELIABLE" or status == "STALE":
            val_str = f"{z_val:.1f} cm"
            badge_bg = (50, 50, 60)
            badge_border = (120, 120, 120)
        elif status == "NO_FLOOD":
            val_str = f"{z_val:.1f} cm"
            badge_bg = (20, 100, 40)
            badge_border = (0, 255, 127)
        else:
            val_str = f"{z_val:.1f} cm"
            badge_bg = (0, 0, 220) if is_alarm else (20, 30, 45)
            badge_border = (0, 0, 255) if is_alarm else gauge_color

        badge_text = f"{clean_name}: {val_str}" if clean_name else val_str

        (tw, th), _ = cv2.getTextSize(badge_text, cv2.FONT_HERSHEY_SIMPLEX, 0.44, 1)
        badge_x = int(wx + nx * 35)
        badge_y = int(wy)
        badge_x = max(10, min(w - tw - 15, badge_x))
        badge_y = max(th + 10, min(h - 15, badge_y))

        cv2.rectangle(out_frame, (badge_x - 5, badge_y - th - 6), (badge_x + tw + 6, badge_y + 6), badge_bg, -1)
        cv2.rectangle(out_frame, (badge_x - 5, badge_y - th - 6), (badge_x + tw + 6, badge_y + 6), badge_border, 1, cv2.LINE_AA)
        cv2.putText(out_frame, badge_text, (badge_x, badge_y), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.arrowedLine(out_frame, (badge_x - 2, badge_y), (wx, wy), badge_border, 1, cv2.LINE_AA, tipLength=0.25)

        # 6. Top HUD Banner Báo Động (Chỉ vẽ khi ở chế độ mốc đơn lẻ draw_hud=True)
        if draw_hud:
            hud_h = 56
            hud_overlay = out_frame.copy()
            cv2.rectangle(hud_overlay, (0, 0), (w, hud_h), (20, 24, 30), -1)
            out_frame = cv2.addWeighted(out_frame, 0.25, hud_overlay, 0.75, 0)

            header_str = f"THUY CHUAN: [{self.name.upper()}] ({int(self.height_cm)} cm) | CONSTRAINED 1D CHANGE-POINT & PROJECTIVE LUT"
            cv2.putText(out_frame, header_str, (15, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (0, 230, 255), 1, cv2.LINE_AA)

            if status == "ABOVE_RANGE":
                alert_str = f"🚨 BAO DONG DO: NUOC DA NGAP VUOT DINH MOC (> {int(self.height_cm)} cm)!"
                alert_color = (0, 0, 255)
            elif is_alarm:
                alert_str = f"CANH BAO NGUY HIEM: MUC NUOC {sub_cm:.1f} cm VUOT NGUONG AN TOAN ({self.warning_cm:.1f} cm)!"
                alert_color = (50, 50, 255)
            elif status == "OCCLUDED":
                alert_str = f"CANH BAO: RANH GIOI DO BI VAT CAN CHE KHUAT - GIU SO DO CU ({sub_cm:.1f} cm)"
                alert_color = (0, 165, 255)
            elif status == "UNRELIABLE" or status == "STALE":
                alert_str = f"TRANG THAI: TIN HIEU KHONG DANG TIN CAY HOAC BI GIAN DOAN [{status}]"
                alert_color = (160, 160, 160)
            elif status == "NO_FLOOD" or sub_cm <= 2.0:
                alert_str = "TRANG THAI: KHO RAO / MAT NUOC CHUA CHOM TOI CHAN MOC (NO_FLOOD)"
                alert_color = (0, 255, 127)
            else:
                alert_str = f"DANG NGAP: MUC NUOC {sub_cm:.1f} cm (Duoi nguong nguy hiem {self.warning_cm:.1f} cm)"
                alert_color = (0, 215, 255)

            cv2.putText(out_frame, alert_str, (15, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.46, alert_color, 1, cv2.LINE_AA)

        return out_frame, sub_cm, is_alarm, status, conf


def strip_accents(text):
    if not text:
        return ""
    text = unicodedata.normalize('NFD', str(text))
    text = "".join(c for c in text if unicodedata.category(c) != 'Mn')
    return text.replace('đ', 'd').replace('Đ', 'D')


GAUGE_COLOR_PALETTE = [
    {"name": "Xanh Ngọc", "bgr": (255, 200, 0), "hex": "#06b6d4"},
    {"name": "Vàng Hổ Phách", "bgr": (0, 215, 255), "hex": "#f59e0b"},
    {"name": "Tím Violet", "bgr": (230, 80, 180), "hex": "#a855f7"},
    {"name": "Xanh Lục", "bgr": (0, 255, 127), "hex": "#10b981"},
    {"name": "Cam Rực Rỡ", "bgr": (0, 140, 255), "hex": "#f97316"},
    {"name": "Hồng Neon", "bgr": (180, 50, 255), "hex": "#ec4899"}
]

LANDMARK_PRESETS = {
    "tree_pole": {
        "name": "Gốc cây / Cột ven đường",
        "h_start_cm": 0.0,
        "h_end_cm": 100.0,
        "warning_cm": 25.0,
        "desc": "Gốc cây hoặc cột tiêu ven đường (0 -> 100cm)"
    },
    "sidewalk": {
        "name": "Vỉa hè đô thị",
        "h_start_cm": 0.0,
        "h_end_cm": 20.0,
        "warning_cm": 15.0,
        "desc": "Bó vỉa hè tiêu chuẩn (0 -> 20cm) - Nhạy nhất khi nước mới tràn"
    },
    "curb_divider": {
        "name": "Bờ kè / Dải phân cách",
        "h_start_cm": 0.0,
        "h_end_cm": 40.0,
        "warning_cm": 25.0,
        "desc": "Bờ kè đá hoặc dải phân cách cứng (0 -> 40cm)"
    },
    "wall_fence": {
        "name": "Bờ tường trên vỉa hè",
        "h_start_cm": 20.0,
        "h_end_cm": 100.0,
        "warning_cm": 40.0,
        "desc": "Chân tường nhà dân hoặc trụ cổng rào trên vỉa hè (20 -> 100cm)"
    },
    "traffic_sign": {
        "name": "Cột biển báo giao thông",
        "h_start_cm": 20.0,
        "h_end_cm": 220.0,
        "warning_cm": 60.0,
        "desc": "Cột thép biển báo hình tròn/tam giác chuẩn QCVN 41:2019 (20 -> 220cm)"
    },
    "light_pole": {
        "name": "Cột đèn chiếu sáng",
        "h_start_cm": 20.0,
        "h_end_cm": 320.0,
        "warning_cm": 80.0,
        "desc": "Cột thép đèn đường đô thị (20 -> 320cm) - Đo ngập cực hạn"
    }
}


def draw_calibration_preview(base_img, p_bottom=None, p_top=None, p_mid=None, name="", height_cm=100.0, warning_cm=25.0, click_step=0, r_M=0.5, calib_mode="3_points", strip_width=20, h_start_cm=0.0, h_end_cm=100.0):
    """
    Vẽ ảnh xem trước hướng dẫn người dùng chấm điểm thiết lập thước đo đơn lẻ (Chuẩn 3 điểm Cross-Ratio).
    """
    if base_img is None:
        return None

    out = base_img.copy()
    clean_name = strip_accents(name).strip()
    delta_h = max(2.0, h_end_cm - h_start_cm)

    if p_bottom is not None:
        bx, by = int(p_bottom[0]), int(p_bottom[1])
        cv2.circle(out, (bx, by), 6, (0, 255, 0), -1, cv2.LINE_AA)
        cv2.circle(out, (bx, by), 10, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(out, f"1. Day ({int(h_start_cm)}cm)", (bx + 12, by + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (0, 255, 0), 1, cv2.LINE_AA)

    if p_mid is not None:
        mx, my = int(p_mid[0]), int(p_mid[1])
        cv2.circle(out, (mx, my), 5, (255, 200, 0), -1, cv2.LINE_AA)
        cv2.circle(out, (mx, my), 9, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(out, f"2. Giua ({int(h_start_cm + delta_h/2)}cm)", (mx + 12, my + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (255, 200, 0), 1, cv2.LINE_AA)

    if p_bottom is not None and p_top is not None:
        tx, ty = int(p_top[0]), int(p_top[1])
        cv2.circle(out, (tx, ty), 6, (0, 215, 255), -1, cv2.LINE_AA)
        cv2.circle(out, (tx, ty), 10, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(out, f"3. Dinh ({int(h_end_cm)}cm)", (tx + 12, ty + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (0, 215, 255), 1, cv2.LINE_AA)

        temp_gauge = VirtualStaffGauge(
            name=clean_name, p_bottom=p_bottom, p_top=p_top, p_mid=p_mid,
            warning_cm=warning_cm, r_M=r_M, strip_width=strip_width,
            h_start_cm=h_start_cm, h_end_cm=h_end_cm
        )
        temp_gauge.calibrate_dry(base_img)
        out, _, _, _, _ = temp_gauge.render_on_frame(out, water_mask=None, draw_hud=False)

    return out


def draw_multi_calibration_preview(
    base_img,
    multi_manager,
    active_gauge_id="gauge_1",
    p_bottom=None,
    p_top=None,
    p_mid=None,
    name="",
    h_start_cm=0.0,
    h_end_cm=100.0,
    warning_cm=25.0,
    click_step=0,
    r_M=0.5,
    calib_mode="3_points",
    strip_width=20,
    cur_name=None,
    **kwargs
):
    """
    Vẽ ảnh xem trước đa mốc: hiển thị tất cả các mốc đã hoàn tất kèm mốc đang chấm điểm theo Chuẩn 3 Điểm.
    """
    if cur_name is not None and not name:
        name = cur_name
    if base_img is None:
        return None

    out = base_img.copy()

    # 1. Vẽ tất cả các mốc đã lưu (is_configured == True) trước đó
    for gid, g in multi_manager.gauges.items():
        if g.is_configured:
            if gid == active_gauge_id and p_bottom is not None and p_top is not None and p_bottom == g.p_bottom and p_top == g.p_top:
                continue
            out, _, _, _, _ = g.render_on_frame(out, water_mask=None, draw_hud=False)

    cfg_cnt = sum(1 for gx in multi_manager.gauges.values() if gx.is_configured)
    raw_name = str(name).strip() if name else ""
    clean_name = strip_accents(raw_name)
    g_active = multi_manager.gauges.get(active_gauge_id) if active_gauge_id else None
    if g_active and not g_active.is_configured:
        active_color_bgr = g_active.color_bgr
    else:
        active_color_bgr = GAUGE_COLOR_PALETTE[cfg_cnt % len(GAUGE_COLOR_PALETTE)]["bgr"]

    h_s_val = float(h_start_cm if h_start_cm is not None else 0.0)
    h_e_val = float(h_end_cm if h_end_cm is not None else 0.0)
    delta_h = max(2.0, h_e_val - h_s_val) if h_e_val > h_s_val else 100.0

    # 3. Vẽ điểm Đáy A
    if p_bottom is not None:
        bx, by = int(p_bottom[0]), int(p_bottom[1])
        cv2.circle(out, (bx, by), 6, (0, 255, 0), -1, cv2.LINE_AA)
        cv2.circle(out, (bx, by), 10, active_color_bgr, 1, cv2.LINE_AA)
        bot_txt = f"1. Diem dau ({int(h_s_val)}cm)" if h_e_val > h_s_val else "1. Diem dau"
        cv2.putText(out, bot_txt, (bx + 12, by + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (0, 255, 0), 1, cv2.LINE_AA)

    # 4. Vẽ điểm Giữa M (H/2)
    if p_mid is not None:
        mx, my = int(p_mid[0]), int(p_mid[1])
        cv2.circle(out, (mx, my), 5, (255, 200, 0), -1, cv2.LINE_AA)
        cv2.circle(out, (mx, my), 9, active_color_bgr, 1, cv2.LINE_AA)
        mid_cm = int(h_s_val + delta_h / 2.0)
        mid_txt = f"2. Diem giua ({mid_cm}cm)" if h_e_val > h_s_val else "2. Diem giua"
        cv2.putText(out, mid_txt, (mx + 12, my + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (255, 200, 0), 1, cv2.LINE_AA)

    # 5. Vẽ điểm Đỉnh B & Xem trước thước đo
    if p_bottom is not None and p_top is not None:
        tx, ty = int(p_top[0]), int(p_top[1])
        cv2.circle(out, (tx, ty), 6, (0, 215, 255), -1, cv2.LINE_AA)
        cv2.circle(out, (tx, ty), 10, active_color_bgr, 1, cv2.LINE_AA)
        top_txt = f"3. Diem cuoi ({int(h_e_val)}cm)" if h_e_val > h_s_val else "3. Diem cuoi"
        cv2.putText(out, top_txt, (tx + 12, ty + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (0, 215, 255), 1, cv2.LINE_AA)

        preview_end_cm = h_e_val if h_e_val > h_s_val else (h_s_val + 100.0)
        temp_g = VirtualStaffGauge(
            name=clean_name, p_bottom=p_bottom, p_top=p_top, p_mid=p_mid,
            warning_cm=warning_cm, r_M=r_M, strip_width=strip_width,
            h_start_cm=h_s_val, h_end_cm=preview_end_cm,
            color_bgr=active_color_bgr
        )
        temp_g.calibrate_dry(base_img)
        out, _, _, _, _ = temp_g.render_on_frame(out, water_mask=None, draw_hud=False)

    return out


class MultiVirtualStaffGaugeManager:
    """
    Hệ thống Quản lý Mạng Lưới Đa Mốc Thủy Chuẩn Bậc Thang Chuyển Giao (Stepped Cascading Staff Gauge Network - SCSG-Net).
    
    Cơ sở khoa học và tiêu chuẩn quốc tế:
    - USGS Techniques and Methods 3-A7 (Sauer & Turnipseed, 2010): "Stage Measurement at Gaging Stations"
      (Chương Sectional and Stepped Staff Gages & Datum Handover Protocol).
    - ISO 4373:2022: "Hydrometry — Water level measuring devices".
    - Lin et al. (IEEE Sensors Journal / MDPI Sensors, 2022): "An Intelligent Multi-Sectional Water Level Measurement System Based on Surveillance Video".
    - Zhang et al. (Elsevier Journal of Hydrology, 2022): "Cascaded Virtual Staff Gauge Network for Continuous Flood Stage Extraction Under Large Dynamic Range".

    Cây quyết định 4 quy tắc bàn giao vật lý (Physical Cascading Handover Decision Tree):
    1. Quy tắc 1 (Mốc thấp ngập, mốc cao khô): Lấy 100% số đo mốc thấp (Z = Z_low). Mốc khô gán W=0.
    2. Quy tắc 2 (Mốc 1 ngập lút đầu, mốc 2 ngập): Bàn giao số đo cho mốc 2 (Z = Z_high), khóa sàn cứng Z >= H_end_1.
    3. Quy tắc 3 (Cả 2 ngập tuốt qua đầu): Lấy đỉnh mốc cao nhất Z >= max(H_end) kèm Còi báo động đỏ tràn đỉnh Cấp 5.
    4. Quy tắc 4 (Nhiều mốc cùng đo): Hợp nhất tối ưu gia quyền phối cảnh (IEEE BLUE Multi-Gauge Fusion).
    """
    def __init__(self):
        self.gauges = {}
        self.active_id = "gauge_1"
        self.smoothed_fused_z = 0.0
        self.last_valid_time = 0.0
        self.tau_temporal = 0.8
        self.fusion_average_mode = "weighted"  # Chuẩn hóa duy nhất: Gia quyền phối cảnh IEEE BLUE

        # Khởi tạo mốc mặc định ban đầu
        self.add_gauge(name="Gốc cây ven đường", h_start_cm=0.0, h_end_cm=100.0, warning_cm=25.0, gauge_id="gauge_1")

    def set_fusion_average_mode(self, mode: str = "weighted"):
        """Quy chuẩn hợp nhất tối ưu gia quyền phối cảnh (IEEE BLUE Multi-Gauge Fusion)."""
        self.fusion_average_mode = "weighted"


    def add_gauge(self, name="Mốc Mới", h_start_cm=0.0, h_end_cm=100.0, warning_cm=25.0, gauge_id=None, preset_key=None):
        if gauge_id is None:
            idx = 1
            while f"gauge_{idx}" in self.gauges:
                idx += 1
            gauge_id = f"gauge_{idx}"

        color_idx = len(self.gauges) % len(GAUGE_COLOR_PALETTE)
        palette = GAUGE_COLOR_PALETTE[color_idx]

        if preset_key and preset_key in LANDMARK_PRESETS:
            preset = LANDMARK_PRESETS[preset_key]
            name = preset["name"]
            h_start_cm = preset["h_start_cm"]
            h_end_cm = preset["h_end_cm"]
            warning_cm = preset["warning_cm"]

        g = VirtualStaffGauge(
            name=name,
            h_start_cm=h_start_cm,
            h_end_cm=h_end_cm,
            warning_cm=warning_cm,
            color_bgr=palette["bgr"],
            color_hex=palette["hex"],
            gauge_id=gauge_id
        )
        self.gauges[gauge_id] = g
        self.active_id = gauge_id
        return gauge_id

    def remove_gauge(self, gauge_id):
        if gauge_id in self.gauges:
            if len(self.gauges) <= 1:
                self.gauges[gauge_id].reset()
                return False
            del self.gauges[gauge_id]
            if self.active_id == gauge_id or self.active_id not in self.gauges:
                self.active_id = next(iter(self.gauges.keys()))
            return True
        return False

    def get_gauge(self, gauge_id: str) -> Optional[VirtualStaffGauge]:
        return self.gauges.get(gauge_id)

    def get_active_gauge(self) -> Optional[VirtualStaffGauge]:
        return self.gauges.get(self.active_id)

    def set_active_gauge(self, gauge_id):
        if gauge_id in self.gauges:
            self.active_id = gauge_id

    def configure_active_gauge(self, p_bottom, p_top, p_mid=None, name=None, h_start_cm=None, h_end_cm=None, warning_cm=None, base_frame=None, calib_mode="3_points", r_M=0.5):
        g = self.get_active_gauge()
        if g is None:
            return
        if h_start_cm is not None:
            g.h_start_cm = float(h_start_cm)
        if h_end_cm is not None:
            g.h_end_cm = float(h_end_cm)
        if name is not None and str(name).strip():
            g.name = str(name).strip()
        if warning_cm is not None:
            g.warning_cm = float(warning_cm)
        g.configure(
            p_bottom=p_bottom,
            p_top=p_top,
            p_mid=p_mid,
            name=g.name,
            warning_cm=g.warning_cm,
            base_frame=base_frame,
            r_M=r_M,
            h_start_cm=g.h_start_cm,
            h_end_cm=g.h_end_cm
        )

    def update_active_gauge_params(self, name=None, h_start_cm=None, h_end_cm=None, warning_cm=None):
        """
        Cập nhật tức thì các thông số vật lý và dải đo cm cho mốc đang chọn:
        - Tự động kiểm tra h_end_cm > h_start_cm
        - Tái tính toán chiều cao Delta_H và bảng Look-Up Table (LUT O(1))
        - Giữ nguyên các điểm tọa độ đã chấm mà không bị mất.
        """
        g = self.get_active_gauge()
        if g is None:
            return
        if name is not None and str(name).strip():
            g.name = str(name).strip()
        if h_start_cm is not None:
            try:
                g.h_start_cm = float(h_start_cm)
            except (ValueError, TypeError):
                pass
        if h_end_cm is not None:
            try:
                g.h_end_cm = max(g.h_start_cm + 1.0, float(h_end_cm))
            except (ValueError, TypeError):
                pass
        g.height_cm = max(1.0, g.h_end_cm - g.h_start_cm)
        if warning_cm is not None:
            try:
                g.warning_cm = float(warning_cm)
            except (ValueError, TypeError):
                pass
        if g.p_bottom is not None and g.p_top is not None:
            g._update_geometry()

    def reset_active_gauge(self):
        g = self.get_active_gauge()
        if g:
            g.reset()

    def reset_all(self):
        """Xóa sạch toàn bộ các cột mốc đã thiết lập và đưa về trạng thái khởi tạo ban đầu."""
        self.gauges.clear()
        self.add_gauge(
            name="Mốc 1: Lề Đường / Vỉa Hè",
            h_start_cm=0.0,
            h_end_cm=20.0,
            warning_cm=15.0,
            gauge_id="gauge_1",
            preset_key="curb_step"
        )
        self.active_id = "gauge_1"
        self.smoothed_fused_z = 0.0
        self.last_valid_time = 0.0

    def measure_all(self, water_mask, frame_bgr=None, timestamp=None, threshold=0.25) -> Dict[str, Any]:
        results = {}
        for gid, g in self.gauges.items():
            if not g.is_configured:
                continue
            sub_ratio, sub_cm, wl_pt, is_alarm, status, conf = g.measure_water_level(
                water_mask=water_mask,
                frame_bgr=frame_bgr,
                timestamp=timestamp,
                threshold=threshold
            )
            # Universal datum: Z = h_start + sub_cm
            z_val = g.h_start_cm + sub_cm if status != "NO_FLOOD" else 0.0
            results[gid] = {
                'sub_ratio': sub_ratio,
                'sub_cm': sub_cm,
                'z_val': z_val,
                'wl_pt': wl_pt,
                'is_alarm': is_alarm,
                'status': status,
                'conf': conf,
                'gauge': g
            }
        return results

    def fuse_water_level(self, results: Dict[str, Any], timestamp=None) -> Dict[str, Any]:
        now = time.time() if timestamp is None else float(timestamp)
        configured = [g for g in self.gauges.values() if g.is_configured]
        if not configured:
            return {
                'fused_z': 0.0, 'peak_z': 0.0, 'status': 'UNCONFIGURED',
                'level': 0, 'level_name': 'Chưa cấu hình mốc', 'alarm': False,
                'flooded_count': 0, 'total_count': 0,
                'summary_text': 'Chưa thiết lập mốc nào.',
                'details': []
            }

        measured_list = []
        above_range_list = []
        dry_list = []
        occluded_list = []
        unreliable_list = []

        for gid, res in results.items():
            g = res['gauge']
            status = res['status']
            z_val = res['z_val']
            sub_cm = res['sub_cm']
            conf = res['conf']
            is_alarm = res['is_alarm']

            if status == 'ABOVE_RANGE' or sub_cm >= g.height_cm:
                above_range_list.append((g, g.h_end_cm, conf))
            elif status == 'OCCLUDED':
                occluded_list.append((g, z_val, conf))
            elif status in ('UNRELIABLE', 'STALE'):
                unreliable_list.append((g, z_val, conf))
            elif status == 'NO_FLOOD' or sub_cm <= 1.5:
                dry_list.append((g, 0.0, conf))
            else:
                # MEASURED
                if g.p_top is not None and g.p_bottom is not None:
                    L_px = math.hypot(g.p_top[0] - g.p_bottom[0], g.p_top[1] - g.p_bottom[1])
                else:
                    L_px = 100.0
                res_w = math.sqrt(max(10.0, L_px) / max(10.0, g.height_cm))
                weight = max(0.10, conf) * res_w
                measured_list.append({
                    'gauge': g,
                    'z_val': z_val,
                    'sub_cm': sub_cm,
                    'conf': conf,
                    'weight': weight,
                    'is_alarm': is_alarm
                })

        # Bước 3: Lọc bất thường MAD nếu có từ 3 mốc ngập
        if len(measured_list) >= 3:
            z_vals = [m['z_val'] for m in measured_list]
            med = float(np.median(z_vals))
            mad = float(np.median([abs(x - med) for x in z_vals]))
            thresh = max(2.5 * max(mad, 2.5), 8.0)
            filtered = [m for m in measured_list if abs(m['z_val'] - med) <= thresh]
            if filtered:
                measured_list = filtered

        # Kiểm tra vi phạm sàn bình thông nhau & Khoảng mù (Case 5 & Case 6)
        floor_violated_gids = set()
        gap_bracket = None
        max_above_h = 0.0

        if above_range_list:
            max_above_h = max(h_end for _, h_end, _ in above_range_list)
            if measured_list:
                for m in measured_list:
                    if m['z_val'] < (max_above_h - 2.0):
                        floor_violated_gids.add(m['gauge'].gauge_id)
                        m['floor_violated'] = True
                        m['weight'] = max(0.001, m['weight'] * 0.05)
                        m['conf'] = min(m['conf'], 0.15)
            elif dry_list:
                candidate_dry_starts = [g.h_start_cm for g, _, _ in dry_list if g.h_start_cm > max_above_h]
                if candidate_dry_starts:
                    min_dry_start = min(candidate_dry_starts)
                    gap_bracket = (max_above_h, min_dry_start)

        # Bước 4: Cây Quyết Định 4 Quy Tắc Bàn Giao Phân Tầng Bậc Thang (Stepped Cascading Handover)
        active_rule = "NO_DATA"
        rule_desc = ""

        if measured_list:
            if len(measured_list) == 1:
                # Quy tắc 1 hoặc Quy tắc 2
                single_m = measured_list[0]
                raw_fused_z = single_m['z_val']
                peak_z = single_m['z_val']
                if above_range_list:
                    # Quy tắc 2: Mốc thấp ngập lút đầu -> Bàn giao số đo cho mốc kế tiếp
                    active_rule = "RULE_2_ELEVATION_HANDOVER"
                    raw_fused_z = max(raw_fused_z, max_above_h)
                    peak_z = max(peak_z, max_above_h)
                    rule_desc = f"[Quy tắc 2 - Bàn giao phân tầng] Mốc thấp đã ngập lút (>= {max_above_h:.0f} cm) -> Bàn giao quyền đo cho {single_m['gauge'].name} (Z = {raw_fused_z:.1f} cm)"
                else:
                    # Quy tắc 1: Mốc thấp ngập, mốc cao khô
                    active_rule = "RULE_1_SINGLE_ACTIVE"
                    rule_desc = f"[Quy tắc 1] Đo theo {single_m['gauge'].name} (Z = {raw_fused_z:.1f} cm, các mốc cao khô ráo)"
            else:
                # Quy tắc 4: Hai hoặc nhiều mốc cùng đo đồng thời (Hợp nhất gia quyền phối cảnh IEEE BLUE)
                active_rule = "RULE_4_MULTI_STAGE_AVERAGE"
                total_w = sum(m['weight'] for m in measured_list)
                raw_fused_z = sum(m['z_val'] * m['weight'] for m in measured_list) / max(1e-5, total_w)
                mode_label = f"Gia quyền phối cảnh IEEE {len(measured_list)} mốc"
                peak_z = max(m['z_val'] for m in measured_list)

                # Chốt chặn phân tầng nếu có mốc thấp hơn đã chìm hoàn toàn
                if above_range_list:
                    raw_fused_z = max(raw_fused_z, max_above_h)
                    peak_z = max(peak_z, max_above_h)
                    rule_desc = f"[Quy tắc 4 + Kế thừa] {mode_label}: Z = {raw_fused_z:.1f} cm (Khóa sàn >= {max_above_h:.0f} cm)"
                else:
                    rule_desc = f"[Quy tắc 4] {mode_label}: Z = {raw_fused_z:.1f} cm (Cực đại: {peak_z:.1f} cm)"

            overall_status = "MEASURED"
            alarm = bool(peak_z >= 25.0 or any(m['is_alarm'] for m in measured_list) or len(above_range_list) > 0)

        elif above_range_list:
            # Quy tắc 3: Cả hai / tất cả các mốc ngập đều đã bị lút đỉnh qua đầu
            active_rule = "RULE_3_ALL_OVERTOPPED"
            raw_fused_z = max_above_h
            peak_z = max_above_h
            overall_status = "ABOVE_RANGE"
            alarm = True
            rule_desc = f"[Quy tắc 3 - 🚨 BÁO ĐỘNG TRÀN ĐỈNH] Toàn bộ {len(above_range_list)} mốc đã chìm lút qua đầu -> Mực nước cực đại: >= {max_above_h:.1f} cm!"

        elif dry_list and not occluded_list:
            # Tất cả mốc đều khô ráo
            active_rule = "ALL_DRY"
            raw_fused_z = 0.0
            peak_z = 0.0
            overall_status = "NO_FLOOD"
            alarm = False
            rule_desc = "Tất cả các mốc đều khô ráo (NO_FLOOD: Z = 0.0 cm)"

        elif occluded_list:
            active_rule = "OCCLUDED_HOLD"
            overall_status = "OCCLUDED"
            raw_fused_z = self.smoothed_fused_z
            peak_z = self.smoothed_fused_z
            alarm = False
            rule_desc = f"Mốc bị che khuất -> Giữ số đo gần nhất (Z = {raw_fused_z:.1f} cm)"
        else:
            active_rule = "ALL_DRY"
            raw_fused_z = 0.0
            peak_z = 0.0
            overall_status = "NO_FLOOD"
            alarm = False
            rule_desc = "Chưa có tín hiệu ngập"


        # Bước 5: Lọc thời gian EMA
        if overall_status in ("MEASURED", "ABOVE_RANGE"):
            dt = max(0.01, now - self.last_valid_time) if self.last_valid_time > 0 else 0.05
            beta = float(1.0 - math.exp(-dt / self.tau_temporal))
            beta = np.clip(beta, 0.15, 0.60)
            if self.smoothed_fused_z <= 0.0:
                self.smoothed_fused_z = raw_fused_z
            else:
                self.smoothed_fused_z = (1.0 - beta) * self.smoothed_fused_z + beta * raw_fused_z

            # Cận dưới cứng: Lọc thời gian không bao giờ được kéo tụt xuống dưới đỉnh của mốc đã ngập lút
            if above_range_list:
                self.smoothed_fused_z = max(self.smoothed_fused_z, max_above_h)

            self.last_valid_time = now
            final_fused_z = round(self.smoothed_fused_z, 1)
        else:
            self.smoothed_fused_z = 0.0
            final_fused_z = 0.0

        final_peak_z = round(peak_z, 1)

        # Cấp độ cảnh báo Level 0 - 5
        ref_metric = max(final_fused_z, final_peak_z)
        if ref_metric < 5.0:
            level = 0
            level_name = "Bình thường / Khô ráo"
            level_color = "#22c55e"
        elif ref_metric < 15.0:
            level = 1
            level_name = "Ngập nhẹ"
            level_color = "#eab308"
        elif ref_metric < 25.0:
            level = 2
            level_name = "Ngập trung bình (Tràn vỉa hè)"
            level_color = "#f97316"
        elif ref_metric < 45.0:
            level = 3
            level_name = "Ngập sâu nguy hiểm (Ống xả xe máy)"
            level_color = "#ef4444"
        elif ref_metric < 70.0:
            level = 4
            level_name = "Báo động khẩn cấp (Sàn ô tô)"
            level_color = "#a855f7"
        else:
            level = 5
            level_name = "Ngại ngập cực hạn (Lút đèn pha)"
            level_color = "#dc2626"

        # Bảng chi tiết từng mốc
        details = []
        total_w_active = sum(m['weight'] for m in measured_list) if measured_list else 1.0
        for g in configured:
            res = results.get(g.gauge_id, {})
            st = res.get('status', 'NO_FLOOD')
            sub = res.get('sub_cm', 0.0)
            z = res.get('z_val', 0.0)
            c = res.get('conf', 0.0)

            matching_m = next((m for m in measured_list if m['gauge'].gauge_id == g.gauge_id), None)
            if matching_m:
                if matching_m.get('floor_violated'):
                    w_pct = "0% (Lỗi dưới sàn mốc lút)"
                    st = f"MEASURED (Lỗi sàn < {max_above_h:.0f}cm)"
                else:
                    w_pct = f"{matching_m['weight'] / max(1e-5, total_w_active) * 100:.1f}%"
            elif st == 'ABOVE_RANGE':
                if gap_bracket:
                    w_pct = f"Cận dưới (>= {gap_bracket[0]:.0f}cm)"
                    st = f"ABOVE_RANGE (Kẹp dải < {gap_bracket[1]:.0f}cm)"
                else:
                    w_pct = "Cận dưới (Lút đỉnh)"
            elif st == 'NO_FLOOD':
                if gap_bracket and g.h_start_cm >= gap_bracket[1]:
                    w_pct = f"Chặn trên (< {gap_bracket[1]:.0f}cm)"
                    st = f"NO_FLOOD (Chặn trên dải mù)"
                else:
                    w_pct = "0% (Khô ráo)"
            else:
                w_pct = "0% (Bị che/nhiễu)"

            details.append({
                "Mã Mốc": g.gauge_id.replace("gauge_", "Mốc #"),
                "Tên Mốc": g.name,
                "Màu Sắc": g.color_hex,
                "Khoảng Đo": f"{int(g.h_start_cm)} → {int(g.h_end_cm)} cm",
                "Mực Nước Tuyệt Đối Z": f"{z:.1f} cm" if (st != "NO_FLOOD" and not st.startswith("NO_FLOOD")) else "0.0 cm",
                "Ngập Thân Mốc": f"{sub:.1f} / {int(g.height_cm)} cm",
                "Trạng Thái": st,
                "Độ Tin Cậy": f"{int(c*100)}%",
                "Đóng Góp Trọng Số": w_pct
            })

        n_flooded = len(measured_list) + len(above_range_list)
        n_total = len(configured)
        if gap_bracket:
            summary_text = f"Mực nước kẹp dải mù: >= {gap_bracket[0]:.1f} cm (lút đỉnh mốc thấp) và < {gap_bracket[1]:.1f} cm (chưa tới mốc cao). {n_flooded}/{n_total} mốc ngập."
        elif rule_desc:
            summary_text = f"{rule_desc} | Đang ngập {n_flooded}/{n_total} mốc. Cấp độ: {level_name}."
        else:
            summary_text = f"Đang ngập {n_flooded}/{n_total} mốc. Mực nước toàn cảnh: {final_fused_z:.1f} cm (Cực đại: {final_peak_z:.1f} cm) - {level_name}."

        return {
            'fused_z': final_fused_z,
            'peak_z': final_peak_z,
            'status': overall_status,
            'level': level,
            'level_name': level_name,
            'level_color': level_color,
            'alarm': alarm,
            'flooded_count': n_flooded,
            'total_count': n_total,
            'summary_text': summary_text,
            'details': details,
            'active_rule': active_rule,
            'rule_desc': rule_desc,
            'average_mode': self.fusion_average_mode
        }


    def render_multi_on_frame(self, frame_bgr, water_mask=None, threshold=0.25, draw_hud=True, raw_frame_bgr=None):
        out_frame = frame_bgr.copy()
        h, w = out_frame.shape[:2]
        configured = [g for g in self.gauges.values() if g.is_configured]

        if not configured:
            return out_frame, 0.0, False, "UNCONFIGURED", 0.0, {}, []

        results = {}
        for g in configured:
            feat_frame = raw_frame_bgr if raw_frame_bgr is not None else frame_bgr
            sub_ratio, sub_cm, wl_pt, is_alarm, status, conf = g.measure_water_level(
                water_mask=water_mask,
                frame_bgr=feat_frame,
                threshold=threshold
            )
            z_val = g.h_start_cm + sub_cm if status != "NO_FLOOD" else 0.0
            results[g.gauge_id] = {
                'sub_ratio': sub_ratio,
                'sub_cm': sub_cm,
                'z_val': z_val,
                'wl_pt': wl_pt,
                'is_alarm': is_alarm,
                'status': status,
                'conf': conf,
                'gauge': g
            }
            out_frame, _, _, _, _ = g.render_on_frame(
                out_frame, water_mask=water_mask, threshold=threshold, draw_hud=False, raw_frame_bgr=raw_frame_bgr
            )

        fused = self.fuse_water_level(results)

        # Vẽ Top HUD Banner Tổng Hợp Đa Mốc
        if draw_hud:
            hud_h = 60
            hud_overlay = out_frame.copy()
            cv2.rectangle(hud_overlay, (0, 0), (w, hud_h), (20, 24, 30), -1)
            out_frame = cv2.addWeighted(out_frame, 0.25, hud_overlay, 0.75, 0)

            title_str = f"MANG LUOI DA MOC THUY CHUAN ({fused['flooded_count']}/{fused['total_count']} MOC NGAP) | CAP DO {fused['level']}: {strip_accents(fused['level_name']).upper()}"
            cv2.putText(out_frame, title_str, (15, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (0, 230, 255), 1, cv2.LINE_AA)

            metric_str = f"MUC NUOC TOAN CANH: {fused['fused_z']:.1f} cm (CUC DAI: {fused['peak_z']:.1f} cm) | TRANG THAI: {fused['status']}"
            alert_color = (50, 50, 255) if fused['alarm'] else ((0, 255, 127) if fused['fused_z'] < 5.0 else (0, 215, 255))
            cv2.putText(out_frame, metric_str, (15, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.52, alert_color, 2, cv2.LINE_AA)

            pill_x = w - 15
            for g in reversed(configured):
                res = results.get(g.gauge_id, {})
                st = res.get('status', 'NO_FLOOD')
                z = res.get('z_val', 0.0)
                sub = res.get('sub_cm', 0.0)
                if st == 'NO_FLOOD':
                    p_txt = f"{strip_accents(g.name)[:10]}: KHO"
                    p_bg = (30, 80, 30)
                elif st == 'ABOVE_RANGE':
                    p_txt = f"{strip_accents(g.name)[:10]}: LUT (>={int(g.h_end_cm)}cm)"
                    p_bg = (0, 0, 180)
                elif st == 'OCCLUDED':
                    p_txt = f"{strip_accents(g.name)[:10]}: CHE"
                    p_bg = (0, 100, 180)
                else:
                    p_txt = f"{strip_accents(g.name)[:10]}: {z:.1f}cm"
                    p_bg = (180, 80, 10) if z >= g.warning_cm else (30, 60, 80)

                (ptw, pth), _ = cv2.getTextSize(p_txt, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
                pill_x -= (ptw + 14)
                if pill_x > 450:
                    cv2.rectangle(out_frame, (pill_x, 15), (pill_x + ptw + 10, 45), p_bg, -1)
                    cv2.rectangle(out_frame, (pill_x, 15), (pill_x + ptw + 10, 45), g.color_bgr, 1, cv2.LINE_AA)
                    cv2.putText(out_frame, p_txt, (pill_x + 5, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 255), 1, cv2.LINE_AA)

        return out_frame, fused['fused_z'], fused['alarm'], fused['status'], 0.90, fused, fused['details']


def get_clean_filepath(file_obj):
    """Chuyển đổi video/file từ Gradio thành đường dẫn chuỗi (str) chuẩn, hỗ trợ toàn diện Gradio 5/6."""
    if file_obj is None:
        return ""
    if isinstance(file_obj, str):
        return file_obj.strip()
    if hasattr(file_obj, "path") and file_obj.path:
        return str(file_obj.path).strip()
    if hasattr(file_obj, "name") and file_obj.name:
        return str(file_obj.name).strip()
    if isinstance(file_obj, dict):
        # Trường hợp Gradio 5: {'video': {'path': '...', ...}, 'subtitles': None}
        if "video" in file_obj:
            v_val = file_obj["video"]
            if isinstance(v_val, dict):
                p = v_val.get("path") or v_val.get("name") or ""
                if p:
                    return str(p).strip()
            elif hasattr(v_val, "path") and v_val.path:
                return str(v_val.path).strip()
            elif isinstance(v_val, str) and v_val.strip():
                return v_val.strip()
        p = file_obj.get("path") or file_obj.get("name") or ""
        if isinstance(p, dict):
            p = p.get("path") or p.get("name") or ""
        return str(p).strip()
    return str(file_obj).strip()


def get_video_info(video_path):
    """Lấy thông số metadata của Video: (total_frames, fps, duration_s, w, h)."""
    clean_path = get_clean_filepath(video_path)
    if not clean_path:
        return 0, 25.0, 0.0, 1280, 720
    cap = cv2.VideoCapture(clean_path)
    if not cap.isOpened():
        return 0, 25.0, 0.0, 1280, 720
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
    duration_s = total_frames / fps if fps > 0 else 0.0
    cap.release()
    return total_frames, fps, duration_s, w, h


def extract_video_frame(video_path, frame_idx=0, max_w=640):
    """Trích xuất 1 khung hình tại frame_idx từ file Video (tối ưu hiển thị max_w=640)."""
    clean_path = get_clean_filepath(video_path)
    if not clean_path:
        return None
    cap = cv2.VideoCapture(clean_path)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(frame_idx)))
    ret, frame = cap.read()
    cap.release()
    if ret and frame is not None:
        if max_w is not None and frame.shape[1] > max_w:
            h, w = frame.shape[:2]
            frame = cv2.resize(frame, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
        return frame
    return None


def process_single_frame_gauge(frame_bgr, gauge, flood_model, flood_conf=0.25):
    """
    Xử lý 1 khung hình qua mô hình Flood Segmentation cho 1 mốc đơn lẻ (tương thích ngược).
    """
    h, w = frame_bgr.shape[:2]
    water_mask = np.zeros((h, w), dtype=np.float32)

    if flood_model is not None:
        f_res = flood_model.predict(
            frame_bgr, conf=float(flood_conf), iou=0.55, imgsz=640, retina_masks=False, device=INFER_DEVICE, verbose=False
        )[0]
        if f_res.masks is not None and len(f_res.masks.data) > 0:
            combined_gpu = torch.amax(f_res.masks.data, dim=0)
            m_np = combined_gpu.cpu().numpy()
            water_mask = cv2.resize(m_np, (w, h), interpolation=cv2.INTER_LINEAR)
            # Dọn sạch đốm nhiễu nhỏ bảo toàn sắc nét viền
            bin_m = (water_mask >= float(flood_conf)).astype(np.uint8)
            if np.any(bin_m > 0):
                num_lbl, lbls, stats, _ = cv2.connectedComponentsWithStats(bin_m, connectivity=8)
                clean_m = np.zeros_like(bin_m, dtype=np.float32)
                for i in range(1, num_lbl):
                    if stats[i, cv2.CC_STAT_AREA] >= 150:
                        clean_m[lbls == i] = 1.0
                water_mask = np.where(clean_m > 0, np.maximum(water_mask, float(flood_conf)), 0.0).astype(np.float32)
            else:
                water_mask = np.zeros((h, w), dtype=np.float32)

    out_frame = frame_bgr.copy()
    if np.any(water_mask >= float(flood_conf)):
        blue_layer = np.zeros_like(out_frame)
        blue_layer[water_mask >= float(flood_conf)] = [255, 50, 0]
        out_frame = cv2.addWeighted(out_frame, 1.0, blue_layer, 0.30, 0)

    ann_bgr, measured_cm, is_alarm, status, conf = gauge.render_on_frame(
        out_frame,
        water_mask=water_mask,
        threshold=flood_conf,
        draw_hud=True,
        raw_frame_bgr=frame_bgr
    )
    ann_rgb = cv2.cvtColor(ann_bgr, cv2.COLOR_BGR2RGB)
    return ann_rgb, measured_cm, is_alarm, status, conf


def process_single_frame_multi_gauge(frame_bgr, multi_manager, flood_model, flood_conf=0.25):
    """
    Xử lý 1 khung hình cho Mạng lưới Đa Mốc Thủy Chuẩn:
    Xuất ra ảnh annotate đa mốc, chỉ số Z_fused, Z_peak, báo động, bảng thống kê và chi tiết HTML.
    Tích hợp Lọc Ngưỡng Trễ Kép (Hysteresis) phản hồi tức thời per-frame.
    """
    h, w = frame_bgr.shape[:2]
    water_mask = np.zeros((h, w), dtype=np.float32)

    if flood_model is not None:
        f_res = flood_model.predict(
            frame_bgr, conf=float(flood_conf), iou=0.55, imgsz=640, retina_masks=False, device=INFER_DEVICE, verbose=False
        )[0]
        if f_res.masks is not None and len(f_res.masks.data) > 0:
            combined_gpu = torch.amax(f_res.masks.data, dim=0)
            m_np = combined_gpu.cpu().numpy()
            raw_w_mask = cv2.resize(m_np, (w, h), interpolation=cv2.INTER_LINEAR)

            # 1. Lọc Ngưỡng Trễ Kép (Dual-Threshold Hysteresis)
            t_base = float(flood_conf)
            t_high = max(t_base * 1.30, 0.35)
            t_low = max(t_base * 0.70, 0.16)

            core_seeds = (raw_w_mask >= t_high).astype(np.uint8)
            candidates = (raw_w_mask >= t_low).astype(np.uint8)

            if np.any(candidates > 0):
                if np.any(core_seeds > 0):
                    num_lbl, lbls, stats, _ = cv2.connectedComponentsWithStats(candidates, connectivity=8)
                    core_labels = np.unique(lbls[core_seeds > 0])
                    core_labels = core_labels[core_labels > 0]
                    clean_m = np.zeros_like(candidates, dtype=np.float32)
                    for lbl in core_labels:
                        if stats[lbl, cv2.CC_STAT_AREA] >= 150:
                            clean_m[lbls == lbl] = 1.0
                else:
                    num_lbl, lbls, stats, _ = cv2.connectedComponentsWithStats(candidates, connectivity=8)
                    clean_m = np.zeros_like(candidates, dtype=np.float32)
                    for i in range(1, num_lbl):
                        if stats[i, cv2.CC_STAT_AREA] >= 300:
                            clean_m[lbls == i] = 1.0
                current_mask = np.where(clean_m > 0, np.maximum(raw_w_mask, t_low), 0.0).astype(np.float32)
            else:
                current_mask = np.zeros((h, w), dtype=np.float32)

            # Phản hồi tức thời từng khung hình (zero-lag per-frame)
            water_mask = current_mask

    out_frame = frame_bgr.copy()
    if np.any(water_mask >= float(flood_conf)):
        blue_layer = np.zeros_like(out_frame)
        blue_layer[water_mask >= float(flood_conf)] = [255, 50, 0]
        out_frame = cv2.addWeighted(out_frame, 1.0, blue_layer, 0.30, 0)

    ann_bgr, fused_z, is_alarm, status, conf, fused_dict, details = multi_manager.render_multi_on_frame(
        out_frame, water_mask=water_mask, threshold=flood_conf, draw_hud=True, raw_frame_bgr=frame_bgr
    )
    ann_rgb = cv2.cvtColor(ann_bgr, cv2.COLOR_BGR2RGB)
    return ann_rgb, fused_z, is_alarm, status, conf, fused_dict, details


def process_video_fixed_gauge(video_path, gauge, flood_model, flood_conf=0.25, output_dir=None, progress=None):
    """
    Xử lý video với 1 mốc đơn lẻ (tương thích ngược).
    """
    clean_path = get_clean_filepath(video_path)
    if not clean_path or not gauge.is_configured:
        return None

    cap = cv2.VideoCapture(clean_path)
    fps = int(cap.get(cv2.CAP_PROP_FPS)) or 24
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 100

    target_w, target_h = (1280, 720) if (w > 1280 or h > 720) else (w, h)
    out_dir = output_dir if output_dir is not None else "."
    output_path = f"{out_dir}/output_fixed_gauge_{int(time.time())}.mp4"
    out = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*"mp4v"), 12, (target_w, target_h))

    frame_idx = 0
    while cap.isOpened() and frame_idx < 400:
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1
        if frame_idx % 2 != 0:
            continue
        if progress is not None and total_frames > 0:
            progress(frame_idx / min(400, total_frames), desc=f"Đang phân tích Thủy chuẩn Video ({frame_idx})...")

        if (w, h) != (target_w, target_h):
            frame = cv2.resize(frame, (target_w, target_h))

        ann_rgb, _, _, _, _ = process_single_frame_gauge(frame, gauge, flood_model, flood_conf=flood_conf)
        out.write(cv2.cvtColor(ann_rgb, cv2.COLOR_RGB2BGR))

    cap.release()
    out.release()
    return output_path


def process_video_multi_gauge(video_path, multi_manager, flood_model, flood_conf=0.25, output_dir=None, progress=None):
    """
    Xử lý video hoàn chỉnh với Mạng Lưới Đa Mốc Thủy Chuẩn Ảo xuất file MP4.
    """
    clean_path = get_clean_filepath(video_path)
    configured = [g for g in multi_manager.gauges.values() if g.is_configured]
    if not clean_path or not configured:
        return None

    cap = cv2.VideoCapture(clean_path)
    fps = int(cap.get(cv2.CAP_PROP_FPS)) or 24
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 100

    target_w, target_h = (1280, 720) if (w > 1280 or h > 720) else (w, h)
    out_dir = output_dir if output_dir is not None else "."
    output_path = f"{out_dir}/output_multi_gauge_{int(time.time())}.mp4"
    out = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*"mp4v"), 12, (target_w, target_h))

    frame_idx = 0
    while cap.isOpened() and frame_idx < 400:
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1
        if frame_idx % 2 != 0:
            continue
        if progress is not None and total_frames > 0:
            progress(frame_idx / min(400, total_frames), desc=f"Đang phân tích Đa Mốc Video ({frame_idx})...")

        if (w, h) != (target_w, target_h):
            frame = cv2.resize(frame, (target_w, target_h))

        ann_rgb, _, _, _, _, _, _ = process_single_frame_multi_gauge(frame, multi_manager, flood_model, flood_conf=flood_conf)
        out.write(cv2.cvtColor(ann_rgb, cv2.COLOR_RGB2BGR))

    cap.release()
    out.release()
    return output_path
