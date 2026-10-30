#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
🌊 VIRTUAL STAFF GAUGE FLOOD DEPTH ESTIMATION ENGINE (TAB 1)
Đo Lường Thủy Văn Đô Thị Bằng Thước Ảo Thủy Chuẩn Cố Định (SCSG-Net):
1. Dựa trên Vật làm mốc (Thủy chuẩn vật thể cố định): Sử dụng thước đo phối cảnh LUT O(1).
2. Hệ Thống Thủy Chuẩn Đa Mốc SCSG-Net: Tự động phát hiện vạch ngập và hợp nhất cao trình.
3. Giao diện HUD X-Ray trực quan: Mặt nạ nước RetinaMask, Vạch chỉ mốc, Shoreline và Banner cảnh báo.
================================================================================
"""

from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional, Any
import numpy as np
import cv2
import time
import math
import torch

from fixed_object_gauge import VirtualStaffGauge

INFER_DEVICE = 0 if torch.cuda.is_available() else "cpu"


@dataclass
class HybridDepthReport:
    master_depth_cm: float
    source: str                 # 'vehicle_wheel', 'virtual_gauge', or 'emergency_pole'
    confidence_pct: int
    alert_level: int            # 0 to 5
    alert_title: str
    traffic_advice: str
    active_vehicles_count: int = 0
    vehicle_depth_cm: Optional[float] = None
    gauge_depth_cm: Optional[float] = None
    road_offset_delta_h: float = 6.5
    has_floating_suspect: bool = False
    details: Dict = field(default_factory=dict)


class AdaptiveHybridArbiter:
    """
    Bộ Điều Phối Trọng Tài Thích Ứng:
    Hợp nhất số đo giữa Phương tiện và Thước đo Mốc cố định.
    Tự học độ chênh cao mặt đường (Delta_h) và điều chuyển kịch bản linh hoạt.
    """

    WARNING_LEVELS = [
        (0, "An toàn - Đường khô ráo", (34, 197, 94), "Bình thường"),
        (1, "Ngập nhẹ (5 - 15 cm)", (234, 179, 8), "Chú ý giảm tốc"),
        (2, "Ngập trung bình (15 - 25 cm)", (249, 115, 22), "Ngập nửa bánh xe"),
        (3, "Ngập sâu nguy hiểm (25 - 45 cm)", (239, 68, 68), "Nguy cơ chết máy"),
        (4, "Báo động khẩn cấp (45 - 70 cm)", (168, 85, 247), "Hạn chế lưu thông"),
        (5, "Ngập cực hạn (> 70 cm)", (220, 38, 38), "Cấm đường khẩn cấp")
    ]

    def __init__(self, default_road_offset_delta_h: float = 0.0):
        self.delta_h = float(default_road_offset_delta_h)
        self.learned_offsets = []
        self.last_valid_vehicle_depth = None
        self.last_valid_gauge_depth = None
        self.prev_flood_mask: Optional[np.ndarray] = None

    def get_alert_info(self, depth_cm: float) -> Tuple[int, str, Tuple[int, int, int], str]:
        """Phân loại 6 cấp độ cảnh báo thủy văn đô thị."""
        if depth_cm < 5.0:
            lvl, title, color, advice = self.WARNING_LEVELS[0]
        elif depth_cm < 15.0:
            lvl, title, color, advice = self.WARNING_LEVELS[1]
        elif depth_cm < 25.0:
            lvl, title, color, advice = self.WARNING_LEVELS[2]
        elif depth_cm < 45.0:
            lvl, title, color, advice = self.WARNING_LEVELS[3]
        elif depth_cm < 70.0:
            lvl, title, color, advice = self.WARNING_LEVELS[4]
        else:
            lvl, title, color, advice = self.WARNING_LEVELS[5]
        return lvl, title, color, advice

    def arbitrate(self, vehicle_detections: List[Dict[str, Any]],
                  virtual_gauge: Optional[VirtualStaffGauge] = None,
                  gauge_depth_cm: Optional[float] = None) -> HybridDepthReport:
        """
        Thực hiện điều phối logic:
        1. Kiểm tra xe bám đất (Active)
        2. Nếu không có xe -> Chuyển sang mốc ảo (Idle)
        3. Nếu xe nổi trôi -> Chuyển sang mốc cao tầng (Takeover)
        """
        # Lọc các xe ngập hợp lệ, ưu tiên xe có cự ly tin cậy (không quá xa ở chân trời)
        reliable_veh_depths = [v['depth_cm'] for v in vehicle_detections if not v.get('is_floating', False) and v.get('depth_cm', 0.0) > 0.0 and v.get('is_distance_reliable', True)]
        if not reliable_veh_depths:
            # Fallback nếu toàn bộ xe trong khung hình đều ở xa
            reliable_veh_depths = [v['depth_cm'] for v in vehicle_detections if not v.get('is_floating', False) and v.get('depth_cm', 0.0) > 0.0]

        valid_veh_depths = reliable_veh_depths
        has_floating = any(v.get('is_floating', False) for v in vehicle_detections)

        # Xác định độ sâu mốc (nếu có)
        g_depth = None
        if gauge_depth_cm is not None and gauge_depth_cm > 0.0:
            g_depth = float(gauge_depth_cm)
        elif virtual_gauge is not None:
            if hasattr(virtual_gauge, 'smoothed_fused_z'):
                g_depth = float(virtual_gauge.smoothed_fused_z)
            elif getattr(virtual_gauge, 'is_configured', False):
                g_depth = float(virtual_gauge.last_measured_cm)


        # ==============================================================
        # KỊCH BẢN 1: Có phương tiện đang di chuyển hợp lệ trên đường
        # ==============================================================
        if len(valid_veh_depths) > 0 and not has_floating:
            # Lấy trung vị độ sâu các xe đang lội nước
            master_depth = float(np.median(valid_veh_depths))
            source = f"vehicle_wheel ({len(valid_veh_depths)} xe)"
            conf = 90

        # ==============================================================
        # KỊCH BẢN 2: Ngập cực hạn xe bị nổi bồng bềnh / trôi dạt (>60-70cm)
        # ==============================================================
        elif has_floating:
            if g_depth is not None:
                master_depth = g_depth
                source = "emergency_pole_takeover (Xe bi noi)"
                conf = 85
            else:
                master_depth = 75.0
                source = "emergency_buoyancy_estimate"
                conf = 70

        # ==============================================================
        # KỊCH BẢN 3: Đường vắng không có xe / Đêm khuya (Traffic Idle Mode)
        # ==============================================================
        elif g_depth is not None:
            # Đo trực tiếp theo mạng mốc thủy chuẩn cao trình tuyệt đối SCSG-Net [H_start -> H_end]
            master_depth = g_depth
            source = f"virtual_gauge ({g_depth:.1f} cm)"
            conf = 85
        else:
            # Mặc định an toàn
            master_depth = 0.0
            source = "no_traffic_no_gauge"
            conf = 95

        lvl, title, color, advice = self.get_alert_info(master_depth)

        return HybridDepthReport(
            master_depth_cm=float(round(master_depth, 1)),
            source=source,
            confidence_pct=int(conf),
            alert_level=lvl,
            alert_title=title,
            traffic_advice=advice,
            active_vehicles_count=len(vehicle_detections),
            vehicle_depth_cm=float(round(np.median(valid_veh_depths), 1)) if len(valid_veh_depths) > 0 else None,
            gauge_depth_cm=float(round(g_depth, 1)) if g_depth is not None else None,
            road_offset_delta_h=float(round(self.delta_h, 1)),
            has_floating_suspect=has_floating,
            details={
                'num_vehicles_detected': len(vehicle_detections),
                'valid_vehicle_depths': valid_veh_depths,
                'delta_h_learned': self.delta_h
            }
        )


def render_hybrid_xray_hud(img_bgr: np.ndarray,
                           vehicle_reports: List[Dict[str, Any]],
                           virtual_gauge: Optional[VirtualStaffGauge],
                           report: HybridDepthReport,
                           flood_mask: Optional[np.ndarray] = None) -> np.ndarray:
    """
    Vẽ Giao diện X-Ray HUD Tổng hợp Thời gian thực:
    - Mặt nạ phân đoạn nước bán trong suốt (Cyan Tint BGR [255, 50, 0] + Shoreline Contour)
    - BBox xe nổi (Xanh lá)
    - BBox xe chìm bán trong suốt (Cam X-Ray)
    - Vạch tiếp đất ảo dưới nước (Vàng dạ quang GROUND: -X cm)
    - Thước đo thủy chuẩn mốc cố định (GAUGE: Y cm)
    - Banner cảnh báo thời gian thực trên cùng
    """
    hud = img_bgr.copy()
    h_img, w_img = img_bgr.shape[:2]

    # 0. Vẽ Phân Đoạn Mặt Nạ Nước (Water Segmentation Mask & Shorelines)
    if flood_mask is not None:
        try:
            m_max = float(np.max(flood_mask))
            thresh = 0.25 if m_max <= 1.0 else 30.0
            water_bin = (flood_mask > thresh).astype(np.uint8)
            if np.any(water_bin > 0):
                # Lớp màu xanh nước biển bán trong suốt (Cyan BGR: [255, 50, 0])
                water_tint = np.zeros_like(hud)
                water_tint[water_bin > 0] = [255, 50, 0]
                hud = cv2.addWeighted(hud, 1.0, water_tint, 0.40, 0)

                # Vẽ đường viền ranh giới mặt nước (Shorelines)
                contours, _ = cv2.findContours(water_bin, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                valid_cnts = [c for c in contours if cv2.contourArea(c) > 40]
                if valid_cnts:
                    cv2.drawContours(hud, valid_cnts, -1, (255, 160, 20), 1, cv2.LINE_AA)
        except Exception as e:
            print(f"⚠️ Lỗi vẽ mặt nạ nước: {e}")

    overlay = hud.copy()

    # 1. Vẽ các phương tiện & X-Ray BBox
    for v in vehicle_reports:
        vx1, vy1, vx2, vy2 = v['bbox_visible']
        sx1, sy2, sx2, sy_ground = v['bbox_submerged']
        depth = v['depth_cm']
        v_cls = v['class']
        v_orient = v['orientation']
        is_float = v['is_floating']

        # Hộp chìm màu cam trong suốt (chỉ hiển thị khi xe thực sự bị ngập lội nước)
        if sy_ground > vy2 and depth > 0:
            cv2.rectangle(overlay, (sx1, vy2), (sx2, sy_ground), (0, 140, 255), -1)
            cv2.rectangle(hud, (sx1, vy2), (sx2, sy_ground), (0, 165, 255), 1, cv2.LINE_AA)
            cv2.line(hud, (sx1 - 6, sy_ground), (sx2 + 6, sy_ground), (0, 255, 255), 1, cv2.LINE_AA)
            ground_txt = f"-{depth:.1f}cm"
            cv2.putText(hud, ground_txt, (sx1, min(h_img - 10, sy_ground + 15)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 255, 255), 1, cv2.LINE_AA)

        # Hộp nổi màu xanh lá (hoặc đỏ nếu xe bồng bềnh)
        box_color = (0, 0, 255) if is_float else (0, 255, 0)
        cv2.rectangle(hud, (vx1, vy1), (vx2, vy2), box_color, 1, cv2.LINE_AA)

        # Nhãn thông số trên nóc xe
        tag = f"{v_cls.upper()}"
        if depth > 0:
            tag += f": {depth:.1f}cm"
        cv2.putText(hud, tag, (vx1, max(15, vy1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 0), 1, cv2.LINE_AA)

    # Pha trộn overlay bán trong suốt
    cv2.addWeighted(overlay, 0.35, hud, 0.65, 0, hud)

    # 2. Vẽ Thước đo Mốc Cố Định (Hỗ trợ cả Mạng lưới Đa Mốc Bậc Thang SCSG-Net lẫn Mốc Đơn)
    if virtual_gauge is not None:
        if hasattr(virtual_gauge, 'render_multi_on_frame'):
            res = virtual_gauge.render_multi_on_frame(hud, water_mask=flood_mask, draw_hud=False, raw_frame_bgr=img_bgr)
            hud = res[0]
        elif getattr(virtual_gauge, 'is_configured', False):
            bx, by = virtual_gauge.p_bottom
            tx, ty = virtual_gauge.p_top
            g_depth = virtual_gauge.last_measured_cm
            gauge_width = int(getattr(virtual_gauge, 'strip_width', 20))
            w_half = max(2, gauge_width // 2)

            dx = tx - bx
            dy = ty - by
            length_px = math.hypot(dx, dy)
            ratio_h = min(1.0, max(0.0, g_depth / max(1.0, virtual_gauge.height_cm)))

            if length_px >= 5.0:
                ux, uy = dx / length_px, dy / length_px
                nx, ny = -uy, ux

                p1 = (int(bx - nx * w_half), int(by - ny * w_half))
                p2 = (int(bx + nx * w_half), int(by + ny * w_half))
                p3 = (int(tx + nx * w_half), int(ty + ny * w_half))
                p4 = (int(tx - nx * w_half), int(ty - ny * w_half))
                poly = np.array([p1, p2, p3, p4], dtype=np.int32)

                gauge_overlay = hud.copy()
                cv2.fillPoly(gauge_overlay, [poly], (25, 30, 40))
                cv2.addWeighted(gauge_overlay, 0.45, hud, 0.55, 0, hud)
                cv2.polylines(hud, [poly], True, (255, 255, 0), 1, cv2.LINE_AA)

                if ratio_h > 0.01:
                    mx1 = int(bx - nx * w_half + dx * ratio_h)
                    my1 = int(by - ny * w_half + dy * ratio_h)
                    mx2 = int(bx + nx * w_half + dx * ratio_h)
                    my2 = int(by + ny * w_half + dy * ratio_h)
                    sub_poly = np.array([p1, p2, (mx2, my2), (mx1, my1)], dtype=np.int32)
                    sub_overlay = hud.copy()
                    cv2.fillPoly(sub_overlay, [sub_poly], (0, 0, 220))
                    cv2.addWeighted(sub_overlay, 0.40, hud, 0.60, 0, hud)
                    cv2.line(hud, (mx1 - 4, my1), (mx2 + 4, my2), (0, 0, 255), 1, cv2.LINE_AA)
                    wy = int(by + dy * ratio_h)
                else:
                    wy = by
            else:
                wy = by

            cv2.circle(hud, (bx, by), 4, (0, 255, 0), -1, cv2.LINE_AA)
            cv2.circle(hud, (tx, ty), 4, (0, 215, 255), -1, cv2.LINE_AA)

            gauge_lbl = f"{g_depth:.1f} cm"
            cv2.putText(hud, gauge_lbl, (min(w_img - 150, max(10, bx + w_half + 8)), max(25, wy - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 0), 1, cv2.LINE_AA)

    # 3. Top Banner HUD đã được loại bỏ hoàn toàn theo yêu cầu để khung hình video 100% thông thoáng
    # Toàn bộ thông số mực nước ngập, cấp cảnh báo, độ tin cậy và offset tim đường
    # được hiển thị đầy đủ và chuyên nghiệp ở Bảng Thống Kê và Khung Cảnh Báo HTML bên ngoài.
    return hud


def refine_flood_segmentation_mask(
    raw_mask: np.ndarray,
    h: int,
    w: int,
    conf_thresh: float = 0.25,
    prev_mask: Optional[np.ndarray] = None,
    frame_bgr: Optional[np.ndarray] = None
) -> np.ndarray:
    """
    Tinh chỉnh mặt nạ phân đoạn nước ngập bảo toàn mép nước vật lý:
    1. Chuẩn hóa kích thước (w, h).
    2. Cắt bỏ nhiễu vùng trời / mái hiên (10% đỉnh ảnh).
    3. Loại bỏ đốm nhiễu nhỏ (< 150px) KHÔNG dùng MORPH_CLOSE lớn gây nở phình mép nước.
    4. Gradient-Guided Waterline Snapping: Tự động khóa viền mask vào đường mép gradient vật lý (vỉa hè/mép nước).
    5. Giữ biên dạng sắc nét (không làm nhòe Gaussian ra mặt đường khô).
    6. Làm mượt theo thời gian (Temporal EMA).
    """
    if raw_mask.shape[:2] != (h, w):
        mask = cv2.resize(raw_mask, (w, h), interpolation=cv2.INTER_LINEAR)
    else:
        mask = raw_mask.copy()

    # 1. Cắt bỏ vùng trời / mái nhà ở 10% phía trên cùng của khung hình
    sky_cutoff = int(h * 0.10)
    if sky_cutoff > 0:
        mask[:sky_cutoff, :] = 0.0

    # 2. Lọc Ngưỡng Trễ Kép (Dual-Threshold Hysteresis)
    # T_high: Lõi nước sâu chắc chắn 100% (Core Water Seeds)
    # T_low : Vùng mép nước tiềm năng (Candidate Water Margins)
    t_base = float(conf_thresh)
    t_high = max(t_base * 1.30, 0.35)
    t_low = max(t_base * 0.70, 0.16)

    core_seeds = (mask >= t_high).astype(np.uint8)
    candidates = (mask >= t_low).astype(np.uint8)

    if not np.any(candidates > 0):
        return np.zeros((h, w), dtype=np.float32)

    # Nếu có lõi nước: Chỉ giữ lại các vùng candidates có kết nối trực tiếp với core_seeds
    if np.any(core_seeds > 0):
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(candidates, connectivity=8)
        core_labels = np.unique(labels[core_seeds > 0])
        valid_labels = [int(lbl) for lbl in core_labels if lbl > 0 and stats[lbl, cv2.CC_STAT_AREA] >= 50]
        clean_bin = np.isin(labels, valid_labels).astype(np.uint8) if valid_labels else np.zeros_like(candidates, dtype=np.uint8)
    else:
        # Trường hợp nước rất nông: Lọc theo diện tích tối thiểu
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(candidates, connectivity=8)
        valid_labels = [lbl_idx for lbl_idx in range(1, num_labels) if stats[lbl_idx, cv2.CC_STAT_AREA] >= 100]
        clean_bin = np.isin(labels, valid_labels).astype(np.uint8) if valid_labels else np.zeros_like(candidates, dtype=np.uint8)

    if not np.any(clean_bin > 0):
        return np.zeros((h, w), dtype=np.float32)

    # 4. Gradient-Guided Waterline Snapping (Khóa viền vào mép gradient vật lý của vỉa hè & mặt nước)
    if frame_bgr is not None and frame_bgr.shape[:2] == (h, w):
        try:
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            # Tính gradient Sobel độ nét cao
            grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
            grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
            grad_mag = cv2.magnitude(grad_x, grad_y)
            grad_norm = cv2.normalize(grad_mag, None, 0.0, 1.0, cv2.NORM_MINMAX)

            # Xác định dải viền ranh giới (Narrow Boundary Band) trong bán kính 3 pixel
            k3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            dilated = cv2.dilate(clean_bin, k3)
            eroded = cv2.erode(clean_bin, k3)
            boundary_band = (dilated - eroded) > 0

            # Tại dải ranh giới, nếu gradient độ tương phản mạnh (mép vỉa hè/đường),
            # căn chỉnh xác suất theo gradient để mép nước không bị tràn lấn sang vùng khô sáng màu
            strong_edge = (grad_norm > 0.18) & boundary_band
            suppress_spill = strong_edge & (gray > 120) & (clean_bin == 1)
            clean_bin[suppress_spill] = 0
        except Exception:
            pass

    # 5. Tạo mask xác suất sắc nét bên trong vùng nước thực tế (phản hồi tức thời per-frame, zero-lag)
    refined = np.where(clean_bin > 0, np.maximum(mask, t_low), 0.0).astype(np.float32)
    return np.clip(refined, 0.0, 1.0)


def process_single_frame_hybrid(frame_bgr: np.ndarray,
                                gauge: Optional[VirtualStaffGauge],
                                arbiter: AdaptiveHybridArbiter,
                                flood_model: Any,
                                vehicle_model: Any = None,
                                flood_conf: float = 0.25,
                                veh_conf: float = 0.35,
                                mode: str = "Cột mốc",
                                is_streaming: bool = False,
                                return_bgr: bool = False) -> Tuple[np.ndarray, Any, str, HybridDepthReport]:
    """
    Xử lý một khung hình hoàn chỉnh qua pipeline đo độ sâu theo Thủy chuẩn Cột mốc.
    Tối ưu hóa thời gian thực siêu tốc khi is_streaming=True (imgsz=640, retina_masks=False).
    """
    import pandas as pd
    from fixed_object_gauge import process_single_frame_gauge

    h, w = frame_bgr.shape[:2]

    infer_imgsz = 640 if is_streaming else 800
    infer_retina = False if is_streaming else True
    use_half = bool(INFER_DEVICE != "cpu" and torch.cuda.is_available())

    # 1. Dự đoán mặt nạ nước trên GPU (Tối ưu tốc độ cao khi Streaming)
    flood_mask = np.zeros((h, w), dtype=np.float32)
    if flood_model is not None:
        try:
            with torch.inference_mode():
                f_res = flood_model.predict(
                    frame_bgr,
                    conf=float(flood_conf),
                    iou=0.55,
                    imgsz=infer_imgsz,
                    half=use_half,
                    retina_masks=infer_retina,
                    device=INFER_DEVICE,
                    verbose=False
                )[0]
            prev_m = getattr(arbiter, "prev_flood_mask", None) if arbiter is not None else None
            if f_res.masks is not None and len(f_res.masks.data) > 0:
                combined_gpu = torch.amax(f_res.masks.data, dim=0)
                m_np = combined_gpu.cpu().numpy()
                flood_mask = refine_flood_segmentation_mask(
                    m_np, h, w, conf_thresh=float(flood_conf), prev_mask=prev_m, frame_bgr=frame_bgr
                )
            elif prev_m is not None and prev_m.shape == (h, w):
                # Giảm dần mượt mà khi mất tín hiệu đột ngột 1 frame
                flood_mask = np.where(prev_m * 0.35 >= float(flood_conf), prev_m * 0.35, 0.0).astype(np.float32)
            if arbiter is not None:
                arbiter.prev_flood_mask = flood_mask
        except Exception as e:
            print(f"⚠️ Lỗi suy luận Flood Model: {e}")

    # 2. Đo theo mốc thủy chuẩn cố định
    g_measured_cm = None
    g_status = "UNCONFIGURED"
    g_rule_desc = ""
    active_gauge = gauge
    if active_gauge is not None:
        if hasattr(active_gauge, 'measure_all') and hasattr(active_gauge, 'fuse_water_level'):
            # Là MultiVirtualStaffGaugeManager (Đa mốc bậc thang SCSG-Net)
            try:
                all_res = active_gauge.measure_all(water_mask=flood_mask, frame_bgr=frame_bgr)
                fusion_res = active_gauge.fuse_water_level(all_res)
                g_measured_cm = fusion_res['fused_z'] if fusion_res['status'] != 'NO_FLOOD' else 0.0
                g_status = fusion_res['status']
                g_rule_desc = fusion_res.get('rule_desc', '')
            except Exception as e:
                print(f"⚠️ Lỗi đo mạng lưới đa mốc: {e}")
        elif getattr(active_gauge, 'is_configured', False):
            try:
                _, g_measured_cm, _, g_status, _ = process_single_frame_gauge(
                    frame_bgr, active_gauge, flood_model, flood_conf=flood_conf
                )
            except Exception as e:
                print(f"⚠️ Lỗi đo mốc thủy chuẩn: {e}")

    # 3. Trọng tài điều phối kết quả (chỉ sử dụng thủy chuẩn cột mốc)
    report = arbiter.arbitrate([],
                               virtual_gauge=active_gauge,
                               gauge_depth_cm=g_measured_cm)

    # 4. Vẽ HUD X-Ray
    hud_bgr = render_hybrid_xray_hud(frame_bgr,
                                     [],
                                     active_gauge,
                                     report,
                                     flood_mask=flood_mask)
    output_frame = hud_bgr if return_bgr else cv2.cvtColor(hud_bgr, cv2.COLOR_BGR2RGB)

    # 5. Tạo DataFrame thống kê
    alert_tag = report.alert_title.split(':')[1].strip() if ':' in report.alert_title else report.alert_title
    if active_gauge is not None:
        if hasattr(active_gauge, 'gauges'):
            cfg_count = sum(1 for g in active_gauge.gauges.values() if g.is_configured)
            if cfg_count > 0 and g_measured_cm is not None:
                if g_rule_desc:
                    gauge_desc = g_rule_desc
                else:
                    gauge_desc = f"Mạng lưới {cfg_count} mốc SCSG-Net: {g_measured_cm:.1f} cm ({g_status})"
            else:
                gauge_desc = "Chưa thiết lập mốc nào"
        elif getattr(active_gauge, 'is_configured', False) and g_measured_cm is not None:
            gauge_desc = f"Cột mốc: {g_measured_cm:.1f}cm"
        else:
            gauge_desc = "Chưa thiết lập mốc"
    else:
        gauge_desc = "Chưa thiết lập mốc"

    df = pd.DataFrame([{
        "Nguồn Đo Lường": report.source,
        "Độ Sâu Ngập": f"{report.master_depth_cm:.1f} cm",
        "Cấp Báo Động": f"Level {report.alert_level}: {alert_tag}",
        "Độ Tin Cậy": f"{report.confidence_pct}%",
        "Thủy Chuẩn Mốc": gauge_desc,
    }])

    # 6. Tạo HTML Cảnh báo
    colors_hex = {
        0: "#22c55e",
        1: "#eab308",
        2: "#f97316",
        3: "#ef4444",
        4: "#a855f7",
        5: "#dc2626"
    }
    alarm_color = colors_hex.get(report.alert_level, "#ef4444")

    alarm_html = f"""
    <div style="background:#1e293b; color:white; padding:10px 14px; border-radius:8px; border-left:5px solid {alarm_color}; display:flex; justify-content:space-between; align-items:center;">
        <span style="color:{alarm_color}; font-size:15px; font-weight:700;">🚨 {report.alert_title}</span>
        <span style="font-size:16px; font-weight:700;">Mực nước: <span style="color:#38bdf8;">{report.master_depth_cm:.1f} cm</span></span>
    </div>
    """

    return output_frame, df, alarm_html, report


def process_video_tab5_hybrid(video_path: str,
                              gauge: Any,
                              arbiter: AdaptiveHybridArbiter,
                              flood_model: Any,
                              vehicle_model: Any,
                              flood_conf: float = 0.25,
                              veh_conf: float = 0.35,
                              output_dir: Optional[str] = None,
                              progress=None,
                              mode: str = "Cả 2") -> Optional[str]:
    """
    Xử lý toàn bộ tệp Video tải lên với Hệ Thống Lai Tab 5 và xuất file MP4.
    """
    from fixed_object_gauge import get_clean_filepath

    clean_path = get_clean_filepath(video_path)
    if not clean_path:
        return None

    cap = cv2.VideoCapture(clean_path)
    if not cap.isOpened():
        return None

    fps = int(cap.get(cv2.CAP_PROP_FPS)) or 24
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 100

    target_w, target_h = (1280, 720) if (w > 1280 or h > 720) else (w, h)
    out_dir = output_dir if output_dir is not None else "."
    output_path = f"{out_dir}/output_tab5_hybrid_{int(time.time())}.mp4"
    out = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*"mp4v"), 12, (target_w, target_h))

    frame_idx = 0
    max_frames_to_export = min(total_frames, 500)

    while cap.isOpened() and frame_idx < max_frames_to_export:
        ret, frame = cap.read()
        if not ret or frame is None:
            break

        frame_idx += 1
        if (frame_idx % 2) != 0:
            continue

        if progress is not None:
            progress(frame_idx / max_frames_to_export, desc=f"Đang xuất video Tab 5 ({frame_idx}/{max_frames_to_export} frames)...")

        hud_rgb, _, _, _ = process_single_frame_hybrid(
            frame, gauge, arbiter, flood_model, vehicle_model,
            flood_conf=flood_conf, veh_conf=veh_conf, mode=mode
        )
        hud_bgr = cv2.cvtColor(hud_rgb, cv2.COLOR_RGB2BGR)
        if (hud_bgr.shape[1], hud_bgr.shape[0]) != (target_w, target_h):
            hud_bgr = cv2.resize(hud_bgr, (target_w, target_h))

        out.write(hud_bgr)

    cap.release()
    out.release()
    return output_path

