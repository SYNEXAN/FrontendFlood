from fastapi.responses import StreamingResponse, JSONResponse
from fastapi import Request
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
🌊 AI-POWERED URBAN WATER LEVEL & DEPTH MONITORING SYSTEM
Hệ Thống Đo Độ Sâu Ngập Lụt Đô Thị Theo Thủy Chuẩn Mốc (Virtual Staff Gauge & SCSG-Net):
  - Mạng Lưới Đa Mốc Thủy Chuẩn Cố Định: Stepped Cascading Staff Gauge Network (SCSG-Net)
    (Tiêu chuẩn USGS TM 3-A7, ISO 4373:2022 & Cây quyết định 4 quy tắc chuyển giao cao trình)
  - Phân Đoạn Mặt Nước: YOLOv8s-seg huấn luyện trên 5,090 ảnh ngập lụt thực tế Việt Nam (chuẩn màu Xanh Dương)
  - Giám Sát Phương Tiện & Ùn Tắc: YOLOv8s (Backbone C2f, High-Recall VN Traffic) + ByteTrack thời gian thực 30+ FPS
  - Triển khai tối ưu trên Hugging Face Spaces & Máy chủ biên thời gian thực
================================================================================
"""

import os
import sys
import time
import base64
import math
import threading
from pathlib import Path
import cv2
import numpy as np
import pandas as pd
import torch
from PIL import Image
import gradio as gr
from ultralytics import YOLO

# Vá lỗi tương thích giữa Gradio 4.44 và Starlette các phiên bản
try:
    import starlette.templating
    _orig_template_response = starlette.templating.Jinja2Templates.TemplateResponse
    def _patched_template_response(self, *args, **kwargs):
        if len(args) >= 1 and isinstance(args[0], str):
            name = args[0]
            context = args[1] if len(args) > 1 and isinstance(args[1], dict) else kwargs.get("context", {})
            request = kwargs.get("request")
            if request is None and isinstance(context, dict):
                request = context.get("request")
            other_kwargs = {k: v for k, v in kwargs.items() if k not in ("request", "name", "context")}
            try:
                return _orig_template_response(self, request=request, name=name, context=context, **other_kwargs)
            except Exception:
                try:
                    return _orig_template_response(self, name, context, **other_kwargs)
                except Exception:
                    pass
        return _orig_template_response(self, *args, **kwargs)
    starlette.templating.Jinja2Templates.TemplateResponse = _patched_template_response
except Exception as _e_patch:
    pass

# Vá lỗi tương thích Pydantic v2 và gradio_client (TypeError: argument of type 'bool' is not iterable)
try:
    import gradio_client.utils as _gc_utils
    _orig_js2py = _gc_utils._json_schema_to_python_type
    def _safe_json_schema_to_python_type(schema, defs):
        if not isinstance(schema, dict):
            return "Any"
        return _orig_js2py(schema, defs)
    _gc_utils._json_schema_to_python_type = _safe_json_schema_to_python_type
except Exception:
    pass

# Hỗ trợ Hugging Face ZeroGPU (NVIDIA A10G)
try:
    import spaces
except ImportError:
    class _MockSpaces:
        @staticmethod
        def GPU(fn=None, duration=None):
            def decorator(f):
                return f
            return decorator(fn) if fn else decorator
    spaces = _MockSpaces()

from fixed_object_gauge import (
    VirtualStaffGauge,
    draw_calibration_preview,
    get_clean_filepath,
    get_video_info,
    extract_video_frame,
    process_single_frame_gauge,
    process_video_fixed_gauge,
    MultiVirtualStaffGaugeManager,
    draw_multi_calibration_preview,
    process_single_frame_multi_gauge,
    process_video_multi_gauge,
    LANDMARK_PRESETS,
    GAUGE_COLOR_PALETTE
)
from adaptive_hybrid_depth_engine import (
    AdaptiveHybridArbiter,
    render_hybrid_xray_hud,
    HybridDepthReport,
    process_single_frame_hybrid,
    process_video_tab5_hybrid
)
from traffic_congestion_engine import (
    TrafficCongestionAnalyzer,
    LatestFrameGrabber,
    draw_traffic_roi_preview,
    order_quad_points,
)

# Cấu hình OpenCV FFMPEG ưu tiên giao thức TCP ổn định cho luồng RTSP
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;6000000"

CANTHO_CAMERAS = [
    {
        "id": "cam-tran-viet-chau",
        "name": "Camera Trần Việt Châu",
        "url": "rtsp://admin:Mekongpathfinder2026@tvc14738.smartddns.tv:554/cam/realmonitor?channel=1&subtype=1",
        "lat": 10.048017,
        "lon": 105.772512,
        "status": "online",
        "address": "Hẻm 38, Đ. Trần Việt Châu",
        "short_loc": "Hẻm 38",
        "desc": "Live stream Trần Việt Châu"
    },
    {
        "id": "cam-vo-van-kiet",
        "name": "Camera Võ Văn Kiệt",
        "url": "rtsp://admin:Mekong2026@mekongpathfinder1pnh.smartddns.tv:554/cam/realmonitor?channel=1&subtype=0",
        "lat": 10.0466199,
        "lon": 105.7554705,
        "status": "online",
        "address": "Hẻm 9 Đ. Phạm Ngọc Hưng, Cần Thơ",
        "short_loc": "Hẻm 9",
        "desc": "Live stream Phạm Ngọc Hưng"
    },
    {
        "id": "cam-nga-tu-3/2-thn",
        "name": "Camera Ngã Tư 3/2 - Trần Hoàng Na",
        "url": "rtsp://admin:Mekong2026@mekongpathfinder02thn.smartddns.tv:41003/cam/realmonitor?channel=1&subtype=0",
        "lat": 10.0161664,
        "lon": 105.7598591,
        "status": "online",
        "address": "86 Đ. 3 Tháng 2, An Bình, Tân An, Cần Thơ",
        "short_loc": "Ngã Tư 3/2 - Trần Hoàng Na",
        "desc": "Live stream Ngã Tư 3/2 - Trần Hoàng Na"
    },
    {
        "id": "cam-tran-hoang-na",
        "name": "Camera Trần Hoàng Na",
        "url": "rtsp://admin:Mekong2026@mekongpathfinder01thn.smartddns.tv:554/cam/realmonitor?channel=1&subtype=0",
        "lat": 10.02049130,
        "lon": 105.75523570,
        "status": "online",
        "address": "Công Ty TAMWIN, Số 54 Đ. A3, Khu dân cư 91B, Tân An, Cần Thơ",
        "short_loc": "Khu Dân Cư 91B",
        "desc": "Live stream Khu Dân Cư 91B"
    },
    {
        "id": "cam-hem-51",
        "name": "Camera Hẻm 51",
        "url": "https://www.youtube.com/watch?v=ABmq2HnzaWk",
        "lat": 10.035311,
        "lon": 105.765444,
        "status": "online",
        "address": "Hẻm 51, Cần Thơ",
        "short_loc": "Hẻm 51",
        "desc": "Live stream Hẻm 51"
    },
    {
        "id": "cam-nguyen-de",
        "name": "Camera Nguyễn Đệ",
        "url": "https://www.youtube.com/watch?v=srJJVT9j-mI",
        "lat": 10.048764,
        "lon": 105.763567,
        "status": "online",
        "address": "Nguyễn Đệ, Cần Thơ",
        "short_loc": "Nguyễn Đệ",
        "desc": "Live stream Nguyễn Đệ"
    },
    {
        "id": "cam-30-4",
        "name": "Camera 30/4",
        "url": "https://www.youtube.com/watch?v=kcCOomwTmYI",
        "lat": 10.012251,
        "lon": 105.756640,
        "status": "online",
        "address": "30/4, Cần Thơ",
        "short_loc": "30/4",
        "desc": "Live stream 30/4"
    }
]

# Thư mục gốc chứa model và dữ liệu
BASE_DIR = Path(__file__).resolve().parent
SAMPLE_VIDEO_PATH = str(BASE_DIR / "examples" / "flood_sample_video.mp4")
CAO_BANG_VIDEO_PATH = str(BASE_DIR / "examples" / "nuoc_lu_cao_bang.mp4")

global_multi_gauge_manager = MultiVirtualStaffGaugeManager()
global_staff_gauge = global_multi_gauge_manager.get_active_gauge()
global_tab5_staff_gauge = VirtualStaffGauge(name="Mốc Vỉa Hè / Cột Đèn Đô Thị", p_bottom=None, p_top=None, height_cm=100.0, warning_cm=25.0)
global_tab5_arbiter = AdaptiveHybridArbiter(default_road_offset_delta_h=0.0)
global_traffic_analyzer = TrafficCongestionAnalyzer(db_path=str(BASE_DIR / "traffic_congestion_history.db"))

# Khởi tạo video mặc định (ưu tiên Video Cao Bằng nếu có sẵn)
DEFAULT_GAUGE_VIDEO = CAO_BANG_VIDEO_PATH if Path(CAO_BANG_VIDEO_PATH).exists() else (SAMPLE_VIDEO_PATH if Path(SAMPLE_VIDEO_PATH).exists() else "")
INIT_GAUGE_FRAME = None
INIT_TOTAL_FRAMES = 0
INIT_FPS = 25.0
if DEFAULT_GAUGE_VIDEO:
    try:
        INIT_TOTAL_FRAMES, INIT_FPS, _, _, _ = get_video_info(DEFAULT_GAUGE_VIDEO)
        f0 = extract_video_frame(DEFAULT_GAUGE_VIDEO, 0)
        if f0 is not None:
            INIT_GAUGE_FRAME = cv2.cvtColor(f0, cv2.COLOR_BGR2RGB)
    except Exception as e:
        print(f"⚠️ Không thể trích xuất khung hình khởi tạo: {e}")

VIETNAM_FLOOD_MODEL_PATH = BASE_DIR / "models" / "vietnam_flood_yolov8s_seg.pt"
VEHICLE_MODEL_PATH = BASE_DIR / "models" / "yolov8s_vehicle.pt"
COCO_VEHICLE_MODEL_PATH = BASE_DIR / "models" / "yolov8n.pt"

print("🚀 Đang khởi động hệ thống AI Giám sát Ngập lụt & Ùn tắc Giao thông...")
is_zero_gpu = bool(os.environ.get("SPACES_ZERO_GPU"))
if torch.cuda.is_available() and not is_zero_gpu:
    device = "cuda:0"
    torch.backends.cudnn.benchmark = True
    gpu_name = torch.cuda.get_device_name(0)
    gpu_vram_mb = int(torch.cuda.get_device_properties(0).total_memory / (1024 ** 2))
    print(f"   [+] Thiết bị suy luận: {device} ({gpu_name} - {gpu_vram_mb} MB VRAM)")
else:
    device = "cpu"
    if is_zero_gpu:
        print("   [+] Chế độ Hugging Face ZeroGPU: GPU được cấp phát động an toàn khi xử lý (@spaces.GPU)")
    else:
        print(f"   [+] Thiết bị suy luận: {device}")

# Tải mô hình Phân đoạn ngập lụt (YOLOv8s chuẩn) & Theo dõi phương tiện (YOLOv8s High-Recall)
flood_model = YOLO(str(VIETNAM_FLOOD_MODEL_PATH)) if VIETNAM_FLOOD_MODEL_PATH.exists() else None
vehicle_model = YOLO(str(VEHICLE_MODEL_PATH)) if VEHICLE_MODEL_PATH.exists() else (YOLO(str(COCO_VEHICLE_MODEL_PATH)) if COCO_VEHICLE_MODEL_PATH.exists() else None)

# Đưa trực tiếp các mô hình lên GPU VRAM và thực hiện Warmup (chỉ khi có GPU cố định, không phải ZeroGPU dynamic)
if torch.cuda.is_available() and not is_zero_gpu:
    try:
        for m_obj in (flood_model, vehicle_model):
            if m_obj is not None:
                m_obj.to(device)
        dummy_img = np.zeros((640, 640, 3), dtype=np.uint8)
        if flood_model is not None:
            flood_model.predict(dummy_img, imgsz=640, retina_masks=True, device=0, verbose=False)
        if vehicle_model is not None:
            vehicle_model.predict(dummy_img, imgsz=640, device=0, verbose=False)
        alloc_mb = torch.cuda.memory_allocated(0) / (1024 ** 2)
        print(f"   [+] Đã nạp mô hình lên GPU VRAM & Warmup hoàn tất (VRAM đang dùng: {alloc_mb:.1f} MB)!")
    except Exception as e:
        print(f"⚠️ Cảnh báo khởi tạo GPU Warmup: {e}")

active_model_name = "vietnam_flood_yolov8s_seg.pt (YOLOv8s-seg 5,090 ảnh VN)" if flood_model is not None else "N/A"
active_veh_name = "yolov8s_vehicle.pt (YOLOv8s Small - High Recall VN Traffic)" if vehicle_model is not None else "N/A"
print(f"   [+] Đã nạp thành công mô hình Flood [{active_model_name}] & Vehicle Tracking [{active_veh_name}] (YOLO + ByteTrack)!")


def get_selected_flood_model(*args, **kwargs):
    """Lấy mô hình phân đoạn nước ngập (YOLOv8s)."""
    return flood_model


def get_gauge_dropdown_choices():
    """Lấy danh sách các mốc thước đo đã lưu trong mạng lưới."""
    return [
        (f"{g.name} [{int(g.h_start_cm)} -> {int(g.h_end_cm)}cm]", gid)
        for gid, g in global_multi_gauge_manager.gauges.items()
        if g.is_configured
    ]


def on_avg_mode_change(mode_val):
    """Thay đổi phương pháp tính trung bình đa mốc (Quy tắc 4 SCSG-Net)."""
    global_multi_gauge_manager.set_fusion_average_mode(mode_val)
    desc = "Bình quân gia quyền phối cảnh (Perspective-Weighted)" if global_multi_gauge_manager.fusion_average_mode == "weighted" else "Trung bình cộng đơn giản (Simple Arithmetic Mean)"
    return f"<div style='color:#38bdf8;'>⚖️ Đã đổi chế độ tính trung bình đa mốc: <b>{desc}</b></div>"


LIVE_STREAM_STATE = {
    "run_id": 0,
    "active": False,
    "flood_conf": 0.25,
    "veh_conf": 0.35,
}



def frame_to_b64(frame_rgb, quality=75, max_w=640):
    if frame_rgb is None:
        return ""
    if isinstance(frame_rgb, str):
        return frame_rgb
    h, w = frame_rgb.shape[:2]
    if w > max_w:
        frame_rgb = cv2.resize(frame_rgb, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_LINEAR)
    frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    _, buf = cv2.imencode('.jpg', frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf).decode('ascii')


def frame_to_html_img(frame_rgb=None, quality=72, max_w=640, stream_url=None):
    if stream_url:
        return f"""<div style="background:#0f172a; border-radius:10px; overflow:hidden; min-height:360px; display:flex; align-items:center; justify-content:center; box-shadow:0 4px 6px -1px rgba(0,0,0,0.4);">
            <img src="{stream_url}" style="width:100%; max-height:480px; object-fit:contain; display:block; margin:0 auto;" onerror="setTimeout(() => {{ this.src='{stream_url}&retry=' + Date.now(); }}, 1000);"/>
        </div>"""
    if frame_rgb is None:
        return """<div style="background:#0f172a; border-radius:10px; padding:60px 20px; text-align:center; color:#94a3b8;">
            <p style="font-size:16px; margin:0;">📺 <b>Chưa có tín hiệu video</b></p>
            <p style="font-size:12px; margin:5px 0 0 0;">Vui lòng tải video hoặc chọn mốc để bắt đầu</p>
        </div>"""
    if isinstance(frame_rgb, str) and (frame_rgb.startswith("<div") or frame_rgb.startswith("<img")):
        return frame_rgb
    b64 = frame_to_b64(frame_rgb, quality=quality, max_w=max_w)
    return f"""<div style="background:#0f172a; border-radius:10px; overflow:hidden; min-height:360px; display:flex; align-items:center; justify-content:center; box-shadow:0 4px 6px -1px rgba(0,0,0,0.4);">
        <img src="data:image/jpeg;base64,{b64}" style="width:100%; max-height:480px; object-fit:contain; display:block; margin:0 auto;"/>
    </div>"""

SERVER_FRAME_CACHE = {
    "tab5_base": extract_video_frame(DEFAULT_GAUGE_VIDEO, 0) if DEFAULT_GAUGE_VIDEO else None,
    "traffic_base": extract_video_frame(DEFAULT_GAUGE_VIDEO, 0) if DEFAULT_GAUGE_VIDEO else None,
    "fusion_base": extract_video_frame(DEFAULT_GAUGE_VIDEO, 0) if DEFAULT_GAUGE_VIDEO else None,
}


def get_state_base_frame(state_data, default=None):
    """Lấy khung hình gốc từ RAM máy chủ, tránh tải 15.4MB qua mạng trên mỗi tương tác/khung hình."""
    if not isinstance(state_data, dict):
        return default
    if state_data.get("base_frame") is not None:
        return state_data["base_frame"]
    key = state_data.get("frame_key")
    if key and key in SERVER_FRAME_CACHE:
        return SERVER_FRAME_CACHE[key]
    return default


def set_state_base_frame(state_data, frame, key=None):
    """Lưu khung hình gốc trên RAM máy chủ và chỉ giữ mã khóa định danh trong gr.State."""
    if not isinstance(state_data, dict):
        return
    state_data.pop("base_frame", None)
    if not key:
        key = state_data.get("frame_key") or "default_base"
    state_data["frame_key"] = key
    SERVER_FRAME_CACHE[key] = frame


DEFAULT_TAB5_STATE = {
    "source_type": "video",
    "video_path": DEFAULT_GAUGE_VIDEO,
    "cam_source": None,
    "total_frames": INIT_TOTAL_FRAMES,
    "fps": INIT_FPS,
    "current_frame_idx": 0,
    "step": 0,
    "frame_key": "tab5_base",
    "p_bot": None,
    "p_mid": None,
    "p_top": None,
    "calib_mode": "3_points",
    "strip_width": 20,
}

DEFAULT_TRAFFIC_STATE = {
    "source_type": "video",
    "video_path": DEFAULT_GAUGE_VIDEO,
    "cam_source": None,
    "cam_title": "Video mặc định",
    "total_frames": INIT_TOTAL_FRAMES,
    "fps": INIT_FPS,
    "current_frame_idx": 0,
    "frame_key": "traffic_base",
    "draft_roi_pts": [],
}

DEFAULT_FUSION_STATE = {
    "source_type": "video",
    "video_path": DEFAULT_GAUGE_VIDEO,
    "cam_source": None,
    "cam_title": "Video mặc định",
    "total_frames": INIT_TOTAL_FRAMES,
    "fps": INIT_FPS,
    "current_frame_idx": 0,
    "frame_key": "fusion_base",
}


def stop_live_stream():
    """Ngắt tức thời mọi vòng lặp phát video / camera AI đang chạy."""
    LIVE_STREAM_STATE["run_id"] += 1
    LIVE_STREAM_STATE["active"] = False


def on_tab5_stop_button():
    """Hàm xử lý khi bấm nút Dừng Lại / Tạm Dừng."""
    stop_live_stream()
    stopped_html = """<div style="background:#0f172a; border-radius:10px; padding:60px 20px; text-align:center; color:#94a3b8;">
        <p style="font-size:16px; margin:0;">⏹️ <b>Đã dừng phát video AI</b></p>
        <p style="font-size:12px; margin:5px 0 0 0;">Bấm 'Bắt đầu dự đoán AI' để phát lại luồng 25–30 FPS</p>
    </div>"""
    return stopped_html, "<div style='color:#f59e0b;'>⏹️ <b>Đã dừng dự đoán AI / phát video.</b></div>"


# ==============================================================================
# HÀM XỬ LÝ SỰ KIỆN TAB 5: HỆ THỐNG LAI THÍCH ỨNG (PHƯƠNG TIỆN + THỦY CHUẨN MỐC)
# Sử dụng Mạng Lưới Đa Mốc Bậc Thang (Stepped Cascading Staff Gauge Network - SCSG-Net)
# ==============================================================================
def on_tab5_video_loaded(video_file, state_data, name, h_start_cm, h_end_cm, strip_width=20, **kwargs):
    """Nạp video lên Tab 5: Ngắt luồng đang chạy (nếu có), reset mốc cũ, trích xuất Frame 0 và khởi tạo thanh tua."""
    stop_live_stream()
    global_multi_gauge_manager.reset_all()
    global_tab5_arbiter.prev_flood_mask = None
    if not video_file:
        return None, gr.Slider(maximum=1, value=0), state_data, "<div style='color:orange;'>Chưa có video.</div>", gr.update(choices=[], value=None)

    clean_path = get_clean_filepath(video_file)
    if not clean_path:
        return None, gr.Slider(maximum=1, value=0), state_data, "<div style='color:red;'>Không tìm thấy tệp video!</div>", gr.update(choices=[], value=None)

    total_frames, fps, duration, w, h = get_video_info(clean_path)
    frame0 = extract_video_frame(clean_path, 0)
    if frame0 is None:
        return None, gr.Slider(maximum=1, value=0), state_data, "<div style='color:red;'>Không thể đọc khung hình đầu tiên của video!</div>", gr.update(choices=[], value=None)

    strip_w = int(strip_width) if strip_width is not None else int(state_data.get("strip_width", 20) if state_data else 20)
    strip_w = max(4, min(100, strip_w))
    mode_str = "3_points"

    prev = draw_multi_calibration_preview(
        frame0,
        global_multi_gauge_manager,
        active_gauge_id=global_multi_gauge_manager.active_id,
        p_bottom=None,
        p_top=None,
        p_mid=None,
        name=name if name else "",
        h_start_cm=h_start_cm,
        h_end_cm=h_end_cm,
        click_step=0,
        calib_mode=mode_str,
        strip_width=strip_w
    )

    SERVER_FRAME_CACHE["tab5_base"] = frame0
    new_state = {
        "source_type": "video",
        "video_path": clean_path,
        "cam_source": None,
        "total_frames": total_frames,
        "fps": fps,
        "current_frame_idx": 0,
        "step": 0,
        "frame_key": "tab5_base",
        "p_bot": None,
        "p_mid": None,
        "p_top": None,
        "calib_mode": mode_str,
        "strip_width": strip_w
    }

    slider_update = gr.Slider(
        minimum=0,
        maximum=max(1, total_frames - 1),
        value=0,
        step=1,
        label="Khung hình video (chọn mốc)",
        interactive=True
    )

    status_msg = f"<div style='color:#38bdf8;'>🎬 <b>Video đã sẵn sàng:</b> {total_frames} frames ({duration:.1f}s, {fps:.0f} FPS). 👉 Hãy nhấp chuột vào ảnh để chấm 3 điểm (Đáy -> Giữa -> Đỉnh)!</div>"
    return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB), slider_update, new_state, status_msg, gr.update(choices=[], value=None)


def on_tab5_image_loaded(img_rgb, state_data, name, h_start_cm, h_end_cm, strip_width=20, **kwargs):
    """Nạp ảnh từ Tab Ảnh sang màn hình bên phải để chấm mốc và dự đoán AI."""
    stop_live_stream()
    global_multi_gauge_manager.reset_all()
    global_tab5_arbiter.prev_flood_mask = None
    if img_rgb is None:
        return gr.skip(), gr.skip(), state_data, "<div style='color:orange;'>Chưa có ảnh.</div>", gr.update(choices=[], value=None)

    frame_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    strip_w = int(strip_width) if strip_width is not None else int(state_data.get("strip_width", 20) if state_data else 20)
    strip_w = max(4, min(100, strip_w))
    mode_str = "3_points"

    prev = draw_multi_calibration_preview(
        frame_bgr,
        global_multi_gauge_manager,
        active_gauge_id=global_multi_gauge_manager.active_id,
        p_bottom=None,
        p_top=None,
        p_mid=None,
        name=name if name else "",
        h_start_cm=h_start_cm,
        h_end_cm=h_end_cm,
        click_step=0,
        calib_mode=mode_str,
        strip_width=strip_w
    )

    SERVER_FRAME_CACHE["tab5_base"] = frame_bgr
    new_state = {
        "source_type": "image",
        "video_path": state_data.get("video_path", "") if state_data else "",
        "cam_source": state_data.get("cam_source") if state_data else None,
        "total_frames": 1,
        "fps": 1.0,
        "current_frame_idx": 0,
        "step": 0,
        "frame_key": "tab5_base",
        "p_bot": None,
        "p_mid": None,
        "p_top": None,
        "calib_mode": mode_str,
        "strip_width": strip_w
    }

    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video (chọn mốc)", interactive=False)
    msg = "<div style='color:#38bdf8;'>🖼️ <b>Ảnh đã sẵn sàng!</b> Nhấp chuột lên ảnh bên phải để chấm 3 điểm mốc hoặc bấm <b>'🚀 BẮT ĐẦU DỰ ĐOÁN AI'</b>.</div>"
    return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB), slider_update, new_state, msg, gr.update(choices=[], value=None)


def resolve_camera_stream_url(raw_url: str):
    """Giải mã đường dẫn Camera (RTSP trực tiếp hoặc giải mã link HLS từ YouTube Live nếu có yt_dlp)."""
    u = str(raw_url).strip()
    if u.isdigit():
        return int(u)
    if "youtube.com" in u.lower() or "youtu.be" in u.lower():
        try:
            import yt_dlp
            ydl_opts = {"format": "best[ext=mp4]/best", "quiet": True, "no_warnings": True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(u, download=False)
                if info and info.get("url"):
                    return info["url"]
        except Exception as e:
            print(f"⚠️ Không thể trích xuất luồng trực tiếp từ YouTube ({u}): {e}")
    return u


def on_tab5_camera_connect(cam_url_str, state_data, name, h_start_cm, h_end_cm, strip_width=20, **kwargs):
    """Ngắt luồng đang chạy, xóa cột mốc của camera cũ và kết nối lại ngay với luồng Camera RTSP / Live Stream mới chọn."""
    stop_live_stream()
    global_multi_gauge_manager.reset_all()
    global_tab5_arbiter.prev_flood_mask = None
    raw_src = str(cam_url_str).strip() if cam_url_str is not None else CANTHO_CAMERAS[0]["url"]
    cam_src = resolve_camera_stream_url(raw_src)

    cam_info = next((c for c in CANTHO_CAMERAS if c["url"] == raw_src), None)
    cam_title = f"{cam_info['name']} ({cam_info['address']})" if cam_info else raw_src

    cap = cv2.VideoCapture(cam_src)
    ret, frame_bgr = (False, None)
    if cap.isOpened():
        # Đọc vài frame đầu để vượt qua buffer I-frame của RTSP
        for _ in range(3):
            ret, frame_bgr = cap.read()
            if ret and frame_bgr is not None:
                break
    cap.release()

    if not ret or frame_bgr is None:
        if state_data:
            state_data["source_type"] = "camera_stream"
            state_data["cam_source"] = cam_src
        return (
            gr.skip(),
            gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video (chọn mốc)", interactive=False),
            state_data,
            f"<div style='color:#ef4444;'>⚠️ Đã ngắt luồng cũ nhưng không thể lấy khung hình từ <b>{cam_title}</b>. Vui lòng kiểm tra kết nối mạng hoặc chọn Camera khác!</div>",
            gr.update(choices=[], value=None)
        )

    strip_w = int(strip_width) if strip_width is not None else int(state_data.get("strip_width", 20) if state_data else 20)
    strip_w = max(4, min(100, strip_w))
    mode_str = "3_points"

    prev = draw_multi_calibration_preview(
        frame_bgr,
        global_multi_gauge_manager,
        active_gauge_id=global_multi_gauge_manager.active_id,
        p_bottom=None,
        p_top=None,
        p_mid=None,
        name=name if name else "",
        h_start_cm=h_start_cm,
        h_end_cm=h_end_cm,
        click_step=0,
        calib_mode=mode_str,
        strip_width=strip_w
    )

    SERVER_FRAME_CACHE["tab5_base"] = frame_bgr
    new_state = {
        "source_type": "camera_stream",
        "video_path": state_data.get("video_path", "") if state_data else "",
        "cam_source": cam_src,
        "total_frames": 1,
        "fps": 25.0,
        "current_frame_idx": 0,
        "step": 0,
        "frame_key": "tab5_base",
        "p_bot": None,
        "p_mid": None,
        "p_top": None,
        "calib_mode": mode_str,
        "strip_width": strip_w
    }

    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video (chọn mốc)", interactive=False)
    msg = f"<div style='color:#22c55e;'>📷 <b>Đã kết nối {cam_title}!</b> (Đã làm mới cột mốc). Hãy chấm 3 điểm mốc trên khung hình rồi bấm <b>'🚀 BẮT ĐẦU DỰ ĐOÁN AI'</b>.</div>"
    return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB), slider_update, new_state, msg, gr.update(choices=[], value=None)


def on_tab5_switch_to_image_tab(img_rgb, state_data, name, h_start_cm, h_end_cm, strip_width=20, **kwargs):
    """Khi người dùng chuyển sang tab Ảnh: tự động ngắt ngay video / dự đoán AI đang chạy."""
    stop_live_stream()
    if img_rgb is not None:
        return on_tab5_image_loaded(img_rgb, state_data, name, h_start_cm, h_end_cm, strip_width=strip_width)

    if state_data:
        state_data["source_type"] = "image"
        state_data["p_bot"] = None
        state_data["p_mid"] = None
        state_data["p_top"] = None
        state_data["step"] = 0

    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video (chọn mốc)", interactive=False)
    msg = "<div style='color:#38bdf8;'>🖼️ <b>Đã chuyển sang tab Ảnh</b> (đã tự động ngắt luồng đang chạy). Hãy tải ảnh lên để bắt đầu!</div>"
    return gr.skip(), slider_update, state_data, msg, gr.skip()


def on_tab5_switch_to_video_tab(video_file, state_data, name, h_start_cm, h_end_cm, strip_width=20, **kwargs):
    """Khi người dùng chuyển sang tab Video: tự động ngắt ngay dự đoán AI / luồng đang chạy và khôi phục khung hình video tĩnh."""
    stop_live_stream()
    if video_file:
        return on_tab5_video_loaded(video_file, state_data, name, h_start_cm, h_end_cm, strip_width=strip_width)

    video_path = (state_data.get("video_path") if state_data else None) or DEFAULT_GAUGE_VIDEO
    if video_path and Path(video_path).exists():
        return on_tab5_video_loaded(video_path, state_data, name, h_start_cm, h_end_cm, strip_width=strip_width)

    if state_data:
        state_data["source_type"] = "video"
    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video (chọn mốc)", interactive=True)
    msg = "<div style='color:#38bdf8;'>🎬 <b>Đã chuyển sang tab Video</b> (đã tự động ngắt luồng đang chạy). Hãy tải tệp video lên!</div>"
    return gr.skip(), slider_update, state_data, msg, gr.skip()


def on_tab5_switch_to_camera_tab(cam_url_str, state_data, name, h_start_cm, h_end_cm, strip_width=20, **kwargs):
    """Khi người dùng chuyển sang tab Camera: tự động ngắt ngay video / dự đoán AI đang chạy."""
    stop_live_stream()
    if state_data:
        state_data["source_type"] = "camera_stream"
        state_data["p_bot"] = None
        state_data["p_mid"] = None
        state_data["p_top"] = None
        state_data["step"] = 0

    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video (chọn mốc)", interactive=False)
    msg = "<div style='color:#38bdf8;'>📷 <b>Đã chuyển sang tab Camera</b> (đã tự động ngắt video / dự đoán AI đang chạy). Chọn Camera và bấm <b>Kết nối</b>!</div>"
    return gr.skip(), slider_update, state_data, msg, gr.skip()


def on_tab5_seek_frame(frame_idx, state_data, name, h_start_cm, h_end_cm, strip_width=20, **kwargs):
    """Tua video đến khung hình frame_idx trên Tab 5, mốc thước đo vẫn đứng yên cố định."""
    mode_str = "3_points"
    strip_w = int(strip_width) if strip_width is not None else int(state_data.get("strip_width", 20) if state_data else 20)
    strip_w = max(4, min(100, strip_w))

    if not state_data or not state_data.get("video_path"):
        return None, state_data

    video_path = state_data["video_path"]
    frame = extract_video_frame(video_path, int(frame_idx))
    if frame is None:
        return None, state_data

    set_state_base_frame(state_data, frame, "tab5_base")
    state_data["current_frame_idx"] = int(frame_idx)
    state_data["strip_width"] = strip_w

    for gx in global_multi_gauge_manager.gauges.values():
        if gx.is_configured:
            gx.calibrate_dry(frame)

    p_bot = state_data.get("p_bot")
    p_mid = state_data.get("p_mid")
    p_top = state_data.get("p_top")
    step = state_data.get("step", 0)

    prev = draw_multi_calibration_preview(
        frame,
        global_multi_gauge_manager,
        active_gauge_id=global_multi_gauge_manager.active_id,
        p_bottom=p_bot,
        p_top=p_top,
        p_mid=p_mid,
        name=name if name else "",
        h_start_cm=h_start_cm,
        h_end_cm=h_end_cm,
        click_step=step,
        calib_mode=mode_str,
        strip_width=strip_w
    )

    return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB), state_data


def on_tab5_mode_change(calib_mode, state_data, name, h_start_cm, h_end_cm, strip_width=20, **kwargs):
    """Xử lý làm mới thiết lập mốc chuẩn theo Quy chuẩn 3 điểm."""
    mode_str = "3_points"
    strip_w = int(strip_width) if strip_width is not None else int(state_data.get("strip_width", 20) if state_data else 20)
    strip_w = max(4, min(100, strip_w))

    if not state_data or get_state_base_frame(state_data) is None:
        return None, state_data, "<div style='color:orange;'>Chưa có khung hình!</div>"

    base_frame = get_state_base_frame(state_data)
    state_data["calib_mode"] = mode_str
    state_data["strip_width"] = strip_w
    state_data["p_bot"] = None
    state_data["p_mid"] = None
    state_data["p_top"] = None
    state_data["step"] = 0

    prev = draw_multi_calibration_preview(
        base_frame,
        global_multi_gauge_manager,
        active_gauge_id=global_multi_gauge_manager.active_id,
        p_bottom=None,
        p_top=None,
        p_mid=None,
        name=name if name else "",
        h_start_cm=h_start_cm,
        h_end_cm=h_end_cm,
        click_step=0,
        calib_mode=mode_str,
        strip_width=strip_w
    )

    msg = "<div style='color:#38bdf8;'>📐 Đang ở <b>Quy chuẩn 3 Điểm Khử Phối Cảnh</b>. Hãy nhấp chuột vào ảnh để chấm điểm.</div>"
    return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB), state_data, msg


def on_tab5_canvas_click(evt: gr.SelectData, state_data, name, h_start_cm, h_end_cm, strip_width=20, **kwargs):
    """Xử lý nhấp chuột trực tiếp trên khung hình: chấm tuần tự 3 điểm (Đáy -> Giữa H/2 -> Đỉnh)."""
    mode_str = "3_points"
    strip_w = int(strip_width) if strip_width is not None else int(state_data.get("strip_width", 20) if state_data else 20)
    strip_w = max(4, min(100, strip_w))

    if not state_data or get_state_base_frame(state_data) is None:
        return None, state_data, "<div style='color:orange;'>Chưa có khung hình để chấm điểm!</div>"

    base_frame = get_state_base_frame(state_data)
    x, y = int(evt.index[0]), int(evt.index[1])
    step = state_data.get("step", 0)

    h_start = float(h_start_cm if h_start_cm is not None else 0.0)
    h_end = float(h_end_cm if h_end_cm is not None else 0.0)
    draft_name = str(name).strip() if (name and str(name).strip()) else ""

    if step == 0 or step >= 3:
        state_data["p_bot"] = (x, y)
        state_data["p_mid"] = None
        state_data["p_top"] = None
        state_data["step"] = 1
        prev = draw_multi_calibration_preview(
            base_frame, global_multi_gauge_manager,
            p_bottom=(x, y), p_top=None, p_mid=None,
            name=draft_name, h_start_cm=h_start, h_end_cm=h_end,
            click_step=1, calib_mode=mode_str, strip_width=strip_w
        )
        msg = f"<div style='color:#38bdf8;'>📍 <b>[Bước 1/3]</b> Đã chấm <b>1. Điểm đầu (Đáy 0cm)</b> tại ({x}, {y}). 👉 Tiếp theo hãy chấm <b>2. Điểm giữa (Vạch H/2)</b> trên ảnh!</div>"
        return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB), state_data, msg
    elif step == 1:
        p_bot = state_data["p_bot"]
        p_mid = (x, y)
        state_data["p_mid"] = p_mid
        state_data["step"] = 2
        prev = draw_multi_calibration_preview(
            base_frame, global_multi_gauge_manager,
            p_bottom=p_bot, p_top=None, p_mid=p_mid,
            name=draft_name, h_start_cm=h_start, h_end_cm=h_end,
            click_step=2, calib_mode=mode_str, strip_width=strip_w
        )
        msg = f"<div style='color:#38bdf8;'>📍 <b>[Bước 2/3]</b> Đã chấm <b>2. Điểm giữa (H/2)</b> tại ({x}, {y}). 👉 Tiếp theo hãy chấm <b>3. Điểm cuối (Đỉnh)</b> trên ảnh!</div>"
        return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB), state_data, msg
    else:
        p_bot = state_data["p_bot"]
        p_mid = state_data["p_mid"]
        p_top = (x, y)
        state_data["p_top"] = p_top
        state_data["step"] = 3
        prev = draw_multi_calibration_preview(
            base_frame, global_multi_gauge_manager,
            p_bottom=p_bot, p_top=p_top, p_mid=p_mid,
            name=draft_name, h_start_cm=h_start, h_end_cm=h_end,
            click_step=3, calib_mode=mode_str, strip_width=strip_w
        )
        msg = "<div style='color:#22c55e;'>✅ <b>[Bước 3/3]</b> Đã chấm đủ 3 điểm (Đáy, Giữa, Đỉnh)! Nhập <b>Tên cột mốc</b>, <b>Điểm đầu (cm)</b>, <b>Điểm cuối (cm)</b> bên dưới rồi bấm <b>'Lưu'</b>.</div>"
        return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB), state_data, msg


def on_tab5_width_change(new_width, state_data, name, h_start_cm, h_end_cm, **kwargs):
    """Phản hồi tương tác kéo thả slider bề rộng thước đo (strip_width: 4px - 100px) trên Tab 5."""
    mode_str = "3_points"
    strip_w = int(new_width) if new_width is not None else 20
    strip_w = max(4, min(100, strip_w))

    if state_data:
        state_data["strip_width"] = strip_w

    base_frame = get_state_base_frame(state_data)
    step = state_data.get("step", 0) if state_data else 0

    # Nếu không đang chấm điểm nháp mới thì cập nhật bề rộng của mốc đang chọn và hiệu chuẩn lại mẫu khô
    if step == 0:
        g = global_multi_gauge_manager.get_active_gauge()
        if g is not None and g.is_configured:
            g.set_strip_width(strip_w, base_frame)

    if base_frame is None:
        base = INIT_GAUGE_FRAME if INIT_GAUGE_FRAME is not None else np.zeros((720, 1280, 3), dtype=np.uint8)
        prev = draw_multi_calibration_preview(base, global_multi_gauge_manager, click_step=0, name=name, h_start_cm=h_start_cm, h_end_cm=h_end_cm, calib_mode=mode_str, strip_width=strip_w)
        return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB), state_data, f"<div style='color:#38bdf8;'>📏 Bề rộng thước đo: <b>{strip_w}px</b></div>"

    p_bot = state_data.get("p_bot")
    p_mid = state_data.get("p_mid")
    p_top = state_data.get("p_top")

    prev = draw_multi_calibration_preview(
        base_frame,
        global_multi_gauge_manager,
        active_gauge_id=global_multi_gauge_manager.active_id,
        p_bottom=p_bot,
        p_top=p_top,
        p_mid=p_mid,
        name=name if name else "",
        h_start_cm=h_start_cm,
        h_end_cm=h_end_cm,
        click_step=step,
        calib_mode=mode_str,
        strip_width=strip_w
    )

    status_msg = f"<div style='color:#38bdf8;'>📏 Bề rộng thước đo: <b>{strip_w}px</b></div>"
    return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB), state_data, status_msg


def on_tab5_save_gauge(state_data, name, h_start_cm, h_end_cm, strip_width=20, **kwargs):
    """Lưu cột mốc vừa chấm vào mạng lưới khi bấm nút 'Lưu', kiểm tra bắt buộc đủ 3 điểm khử phối cảnh."""
    mode_str = "3_points"
    strip_w = int(strip_width) if strip_width is not None else int(state_data.get("strip_width", 20) if state_data else 20)
    strip_w = max(4, min(100, strip_w))

    if not state_data or get_state_base_frame(state_data) is None:
        return gr.skip(), state_data, "<div style='color:#ef4444;'>⚠️ Chưa có khung hình video!</div>", gr.skip(), gr.skip(), gr.skip(), gr.skip()

    base_frame = get_state_base_frame(state_data)
    p_bot = state_data.get("p_bot")
    p_mid = state_data.get("p_mid")
    p_top = state_data.get("p_top")

    if not p_bot or not p_mid or not p_top:
        return (
            gr.skip(),
            state_data,
            "<div style='color:#ef4444;'>⚠️ Vui lòng chấm đủ 3 điểm (<b>1. Đáy</b>, <b>2. Giữa H/2</b>, <b>3. Đỉnh</b>) theo chuẩn khử phối cảnh trước khi bấm <b>Lưu</b>!</div>",
            gr.skip(), gr.skip(), gr.skip(), gr.skip()
        )

    h_start = float(h_start_cm if h_start_cm is not None else 0.0)
    h_end = float(h_end_cm if h_end_cm is not None else 0.0)
    if h_end <= h_start:
        return (
            gr.skip(),
            state_data,
            "<div style='color:#ef4444;'>⚠️ <b>Điểm cuối (cm)</b> phải lớn hơn <b>Điểm đầu (cm)</b>! Vui lòng nhập kích thước hợp lệ.</div>",
            gr.skip(), gr.skip(), gr.skip(), gr.skip()
        )

    cfg_count = sum(1 for gx in global_multi_gauge_manager.gauges.values() if gx.is_configured)
    clean_name = str(name).strip() if (name and str(name).strip()) else f"Cột mốc {cfg_count + 1}"

    # Tìm xem có mốc nào chưa cấu hình sẵn không (ví dụ gauge_1 lúc khởi tạo), nếu không thì tạo mốc mới
    target_g = None
    for gx in global_multi_gauge_manager.gauges.values():
        if not gx.is_configured:
            target_g = gx
            global_multi_gauge_manager.set_active_gauge(gx.gauge_id)
            break

    if target_g is None:
        new_gid = global_multi_gauge_manager.add_gauge(
            name=clean_name, h_start_cm=h_start, h_end_cm=h_end, warning_cm=25.0
        )
        target_g = global_multi_gauge_manager.get_gauge(new_gid)

    target_g.configure(
        p_bottom=p_bot,
        p_top=p_top,
        p_mid=p_mid,
        name=clean_name,
        warning_cm=25.0,
        base_frame=base_frame,
        strip_width=strip_w,
        h_start_cm=h_start,
        h_end_cm=h_end,
        calib_mode=mode_str
    )

    # Reset trạng thái nháp để sẵn sàng chấm cột mốc tiếp theo
    state_data["p_bot"] = None
    state_data["p_mid"] = None
    state_data["p_top"] = None
    state_data["step"] = 0

    prev = draw_multi_calibration_preview(
        base_frame,
        global_multi_gauge_manager,
        active_gauge_id=target_g.gauge_id,
        p_bottom=None,
        p_top=None,
        p_mid=None,
        click_step=0,
        calib_mode=mode_str,
        strip_width=strip_w
    )

    choices = get_gauge_dropdown_choices()
    total_saved = len(choices)
    msg = f"<div style='color:#22c55e;'>✅ Đã lưu <b>{clean_name}</b> [{h_start:g} → {h_end:g} cm] (Tổng: {total_saved} cột mốc). Có thể chấm tiếp cột mốc khác hoặc bấm <b>'🚀 BẮT ĐẦU DỰ ĐOÁN AI'</b>!</div>"
    return (
        cv2.cvtColor(prev, cv2.COLOR_BGR2RGB),
        state_data,
        msg,
        "",
        0,
        0,
        gr.update(choices=choices, value=target_g.gauge_id)
    )


def reset_tab5_gauge_points(state_data, strip_width=20, **kwargs):
    """Hủy các điểm đang chấm dở trên màn hình và đặt lại các ô nhập về mặc định."""
    mode_str = "3_points"
    strip_w = int(strip_width) if strip_width is not None else 20

    if state_data:
        state_data["p_bot"] = None
        state_data["p_mid"] = None
        state_data["p_top"] = None
        state_data["step"] = 0
        state_data["calib_mode"] = mode_str
        state_data["strip_width"] = strip_w

    base = get_state_base_frame(state_data, INIT_GAUGE_FRAME)
    prev = draw_multi_calibration_preview(
        base,
        global_multi_gauge_manager,
        active_gauge_id=global_multi_gauge_manager.active_id,
        p_bottom=None,
        p_top=None,
        p_mid=None,
        click_step=0,
        calib_mode=mode_str,
        strip_width=strip_w
    )
    return (
        cv2.cvtColor(prev, cv2.COLOR_BGR2RGB),
        state_data,
        "<div style='color:#f59e0b;'>🔄 Đã hủy điểm đang chấm. Hãy nhấp chuột vào video để chấm lại!</div>",
        "",
        0,
        0
    )


def on_tab5_reset_all_gauges(state_data, strip_width=20, **kwargs):
    """Xóa sạch toàn bộ các cột mốc đã thiết lập và hủy điểm đang chấm dở."""
    stop_live_stream()
    global_multi_gauge_manager.reset_all()
    mode_str = "3_points"
    strip_w = int(strip_width) if strip_width is not None else 20

    if state_data:
        state_data["p_bot"] = None
        state_data["p_mid"] = None
        state_data["p_top"] = None
        state_data["step"] = 0
        state_data["calib_mode"] = mode_str
        state_data["strip_width"] = strip_w

    base = get_state_base_frame(state_data, INIT_GAUGE_FRAME)
    prev = draw_multi_calibration_preview(
        base,
        global_multi_gauge_manager,
        active_gauge_id=global_multi_gauge_manager.active_id,
        p_bottom=None,
        p_top=None,
        p_mid=None,
        click_step=0,
        calib_mode=mode_str,
        strip_width=strip_w
    )
    canvas_rgb = cv2.cvtColor(prev, cv2.COLOR_BGR2RGB) if prev is not None else None
    return (
        canvas_rgb,
        state_data,
        "<div style='color:#f59e0b;'>🗑️ <b>Đã xóa toàn bộ cột mốc đã thiết lập!</b> Hãy nhấp chuột lên ảnh để chấm cột mốc mới.</div>",
        "",
        0,
        0,
        gr.update(choices=[], value=None)
    )


def on_tab5_remove_active_gauge(active_gid, state_data, **kwargs):
    """Xóa mốc đã lưu khỏi mạng lưới từ Tab 5."""
    mode_str = "3_points"
    if active_gid and active_gid in global_multi_gauge_manager.gauges:
        if len(global_multi_gauge_manager.gauges) > 1:
            global_multi_gauge_manager.remove_gauge(active_gid)
        else:
            global_multi_gauge_manager.gauges[active_gid].reset()

    choices = get_gauge_dropdown_choices()
    new_val = choices[-1][1] if choices else None
    if new_val:
        global_multi_gauge_manager.set_active_gauge(new_val)

    base = get_state_base_frame(state_data, INIT_GAUGE_FRAME)
    p_bot = state_data.get("p_bot") if state_data else None
    p_mid = state_data.get("p_mid") if state_data else None
    p_top = state_data.get("p_top") if state_data else None
    step = state_data.get("step", 0) if state_data else 0
    strip_w = state_data.get("strip_width", 20) if state_data else 20

    prev = draw_multi_calibration_preview(
        base,
        global_multi_gauge_manager,
        active_gauge_id=global_multi_gauge_manager.active_id,
        p_bottom=p_bot,
        p_top=p_top,
        p_mid=p_mid,
        click_step=step,
        calib_mode=mode_str,
        strip_width=strip_w
    )
    canvas_rgb = cv2.cvtColor(prev, cv2.COLOR_BGR2RGB) if prev is not None else None
    msg = f"<div style='color:#f59e0b;'>🗑️ Đã xóa cột mốc. Số cột mốc còn lại: <b>{len(choices)}</b>.</div>"
    return (
        gr.update(choices=choices, value=new_val),
        canvas_rgb,
        state_data,
        msg
    )


def on_tab5_flood_conf_changed(new_conf, state_data):
    """Xử lý khi người dùng kéo thanh trượt ngưỡng phát hiện nước (tab5_flood_conf)."""
    val = float(new_conf) if new_conf is not None else 0.25
    LIVE_STREAM_STATE["flood_conf"] = val

    # Nếu đang phát trực tiếp video hoặc camera: luồng live sẽ tự động dùng ngưỡng mới ở frame kế tiếp
    if LIVE_STREAM_STATE.get("active", False):
        status = f"<div style='color:#38bdf8;'>🎯 <b>Đã cập nhật ngưỡng nước: {val:.2f}</b> (áp dụng ngay vào luồng phát trực tiếp)</div>"
        return gr.skip(), gr.skip(), gr.skip(), status

    # Nếu đang xem ảnh tĩnh hoặc video đã dừng/tua:
    base_frame = get_state_base_frame(state_data)
    if base_frame is None:
        status = f"<div style='color:#38bdf8;'>🎯 <b>Đã thiết lập ngưỡng nước: {val:.2f}</b></div>"
        return gr.skip(), gr.skip(), gr.skip(), status

    active_flood = get_selected_flood_model()
    try:
        hud_rgb, df, alarm_html, report = process_single_frame_hybrid(
            base_frame, global_multi_gauge_manager, global_tab5_arbiter,
            active_flood, None,
            flood_conf=val, mode="Cột mốc"
        )
        h_f, w_f = hud_rgb.shape[:2]
        if w_f > 960:
            scale_ratio = 960.0 / w_f
            hud_rgb = cv2.resize(hud_rgb, (960, int(h_f * scale_ratio)), interpolation=cv2.INTER_LINEAR)
        status_info = f"<div style='color:#22c55e;'>🎯 <b>Đã cập nhật ngưỡng nước: {val:.2f}</b> | Mực nước: <b>{report.master_depth_cm:.1f} cm</b></div>"
        return hud_rgb, df, alarm_html, status_info
    except Exception as e:
        return gr.skip(), gr.skip(), gr.skip(), f"<div style='color:red;'>⚠️ Lỗi cập nhật ngưỡng: {e}</div>"


@spaces.GPU(duration=120)
def run_video_tab5_live(state_data, flood_conf, start_frame_slider):
    """Chạy suy luận trực tiếp trên Ảnh, Video hoặc Camera theo Thủy chuẩn Cột mốc."""
    LIVE_STREAM_STATE["run_id"] += 1
    my_run_id = LIVE_STREAM_STATE["run_id"]
    LIVE_STREAM_STATE["active"] = True
    LIVE_STREAM_STATE["flood_conf"] = float(flood_conf) if flood_conf is not None else 0.25

    active_flood = get_selected_flood_model()
    source_type = state_data.get("source_type", "video") if state_data else "video"

    # Trường hợp 1: Nguồn đầu vào là Ảnh tĩnh (từ Tab Ảnh)
    if source_type == "image":
        base_frame = get_state_base_frame(state_data)
        if base_frame is None:
            LIVE_STREAM_STATE["active"] = False
            yield None, pd.DataFrame(), "<div style='color:red;'>⚠️ Vui lòng tải ảnh lên trước khi dự đoán!</div>", "<div style='color:red;'>⚠️ Chưa có ảnh hợp lệ</div>"
            return

        cur_conf = float(LIVE_STREAM_STATE.get("flood_conf", flood_conf))
        hud_rgb, df, alarm_html, report = process_single_frame_hybrid(
            base_frame, global_multi_gauge_manager, global_tab5_arbiter,
            active_flood, None,
            flood_conf=cur_conf, mode="Cột mốc"
        )
        LIVE_STREAM_STATE["active"] = False
        status_info = f"<div style='color:#22c55e;'>✅ <b>Đã phân tích ảnh hoàn tất!</b> | Mực nước: <b>{report.master_depth_cm:.1f} cm</b></div>"
        yield frame_to_b64(hud_rgb), df, alarm_html, status_info
        return

    # Trường hợp 2: Nguồn đầu vào là luồng Camera trực tiếp (RTSP/HTTP)
    if source_type == "camera_stream":
        cam_src = state_data.get("cam_source", 0) if state_data else 0
        grabber = LatestFrameGrabber(cam_src)
        if not grabber.is_opened:
            grabber.release()
            base_frame = get_state_base_frame(state_data)
            if base_frame is not None and LIVE_STREAM_STATE["active"] and LIVE_STREAM_STATE["run_id"] == my_run_id:
                cur_conf = float(LIVE_STREAM_STATE.get("flood_conf", flood_conf))
                hud_rgb, df, alarm_html, report = process_single_frame_hybrid(
                    base_frame, global_multi_gauge_manager, global_tab5_arbiter,
                    active_flood, None,
                    flood_conf=cur_conf, mode="Cột mốc"
                )
                LIVE_STREAM_STATE["active"] = False
                yield hud_rgb, df, alarm_html, f"<div style='color:#22c55e;'>✅ Đã phân tích khung hình Camera | Mực nước: <b>{report.master_depth_cm:.1f} cm</b></div>"
            else:
                LIVE_STREAM_STATE["active"] = False
                yield None, pd.DataFrame(), "<div style='color:red;'>⚠️ Không thể mở luồng Camera!</div>", "<div style='color:red;'>Lỗi kết nối Camera</div>"
            return

        last_seq = -1
        render_cnt = 0
        last_hud = None
        last_df = pd.DataFrame()
        last_alarm = ""
        try:
            while grabber.is_opened and LIVE_STREAM_STATE["active"] and LIVE_STREAM_STATE["run_id"] == my_run_id:
                ok, frame, _, seq = grabber.read_latest()
                if not ok or frame is None or seq == last_seq:
                    time.sleep(0.008)
                    continue
                last_seq = seq
                if not LIVE_STREAM_STATE["active"] or LIVE_STREAM_STATE["run_id"] != my_run_id:
                    break
                render_cnt += 1
                try:
                    cur_conf = float(LIVE_STREAM_STATE.get("flood_conf", flood_conf))
                    hud_rgb, df, alarm_html, report = process_single_frame_hybrid(
                        frame, global_multi_gauge_manager, global_tab5_arbiter,
                        active_flood, None,
                        flood_conf=cur_conf, mode="Cột mốc"
                    )
                    if not LIVE_STREAM_STATE["active"] or LIVE_STREAM_STATE["run_id"] != my_run_id:
                        break
                    h_f, w_f = hud_rgb.shape[:2]
                    if w_f > 720:
                        scale_ratio = 720.0 / w_f
                        hud_rgb = cv2.resize(hud_rgb, (720, int(h_f * scale_ratio)), interpolation=cv2.INTER_LINEAR)

                    status_info = f"<div style='color:#38bdf8;'>📷 <b>Camera Live (Frame {seq})</b> | Mực nước: <b>{report.master_depth_cm:.1f} cm</b></div>"
                    last_hud, last_df, last_alarm = hud_rgb, df, alarm_html
                    yield frame_to_b64(hud_rgb), df, alarm_html, status_info
                except Exception as frame_err:
                    print(f"⚠️ Lỗi xử lý Camera Frame {seq}: {frame_err}")
                    continue
        except GeneratorExit:
            LIVE_STREAM_STATE["active"] = False
            return
        finally:
            grabber.release()
            if LIVE_STREAM_STATE.get("active", False) and LIVE_STREAM_STATE["run_id"] == my_run_id:
                LIVE_STREAM_STATE["active"] = False
                try:
                    if last_hud is not None:
                        yield frame_to_b64(last_hud), last_df, last_alarm, "<div style='color:#22c55e;'>✅ <b>Đã ngắt luồng Camera!</b></div>"
                except GeneratorExit:
                    return
        return

    # Trường hợp 3: Nguồn đầu vào là Video MP4
    video_raw = state_data.get("video_path") if state_data else None
    if not video_raw and DEFAULT_GAUGE_VIDEO:
        video_raw = DEFAULT_GAUGE_VIDEO

    video_path = get_clean_filepath(video_raw)
    if not video_path or not Path(video_path).exists():
        LIVE_STREAM_STATE["active"] = False
        yield frame_to_html_img(None), pd.DataFrame(), "<div style='color:red;'>⚠️ Vui lòng tải video lên trước khi nhận diện!</div>", "<div style='color:red;'>⚠️ Chưa có video hợp lệ</div>"
        return

    # Lưu cấu hình và đường dẫn video vào LIVE_STREAM_STATE để endpoint MJPEG đọc
    LIVE_STREAM_STATE["video_path"] = video_path
    LIVE_STREAM_STATE["active"] = True
    LIVE_STREAM_STATE["flood_conf"] = float(flood_conf) if flood_conf is not None else 0.25

    # Kích hoạt luồng MJPEG Hardware Streaming chuẩn 25-30 FPS
    cur_conf = float(LIVE_STREAM_STATE.get("flood_conf", 0.25))
    mjpeg_url = f"/api/v1/stream/flood?flood_conf={cur_conf}&t={int(time.time()*1000)}"
    stream_html = frame_to_html_img(stream_url=mjpeg_url)
    status_info = "<div style='color:#22c55e;'>🟢 <b>Đang phát luồng AI Realtime (25–30 FPS MJPEG)</b> | Phần cứng GPU Tesla T4 tăng tốc</div>"
    yield stream_html, pd.DataFrame(), "<div style='color:#22c55e; font-weight:bold;'>🚀 Đang phân tích luồng ngập úng realtime 25–30 FPS...</div>", status_info
    return

# ==============================================================================
# HÀM XỬ LÝ SỰ KIỆN TAB 2: HỆ THỐNG ĐO ĐỘ ÙN TẮC GIAO THÔNG (YOLO + BYTETRACK)
# ==============================================================================
def _render_traffic_base_with_roi(frame_bgr, state_data=None):
    """Vẽ preview khung 4 góc làn đường (hoặc các góc đang chấm dở) lên khung hình gốc."""
    if frame_bgr is None:
        return None
    draft_pts = state_data.get("draft_roi_pts", []) if state_data else []
    prev_bgr = draw_traffic_roi_preview(
        frame_bgr,
        roi_points_norm=global_traffic_analyzer.roi_points_norm,
        draft_points=draft_pts,
        roi_label=global_traffic_analyzer.roi_label,
    )
    return cv2.cvtColor(prev_bgr, cv2.COLOR_BGR2RGB)


def on_traffic_video_loaded(video_file, state_data):
    """Nạp video lên Tab Đo Độ Ùn Tắc: Ngắt luồng đang chạy, trích xuất Frame 0 và khởi tạo thanh tua."""
    stop_live_stream()
    global_traffic_analyzer.reset()
    global_traffic_analyzer.clear_roi()
    if not video_file:
        return None, gr.Slider(maximum=1, value=0), state_data, "<div style='color:orange;'>Chưa có video.</div>"

    clean_path = get_clean_filepath(video_file)
    if not clean_path:
        return None, gr.Slider(maximum=1, value=0), state_data, "<div style='color:red;'>Không tìm thấy tệp video!</div>"

    total_frames, fps, duration, w, h = get_video_info(clean_path)
    frame0 = extract_video_frame(clean_path, 0)
    if frame0 is None:
        return None, gr.Slider(maximum=1, value=0), state_data, "<div style='color:red;'>Không thể đọc khung hình đầu tiên của video!</div>"

    SERVER_FRAME_CACHE["traffic_base"] = frame0
    new_state = {
        "source_type": "video",
        "video_path": clean_path,
        "cam_source": None,
        "cam_title": Path(clean_path).name,
        "total_frames": total_frames,
        "fps": fps,
        "current_frame_idx": 0,
        "frame_key": "traffic_base",
        "draft_roi_pts": [],
    }

    slider_update = gr.Slider(
        minimum=0,
        maximum=max(1, total_frames - 1),
        value=0,
        step=1,
        label="Khung hình video",
        interactive=True
    )

    status_msg = f"<div style='color:#38bdf8;'>🎬 <b>Video đã sẵn sàng:</b> {total_frames} frames ({duration:.1f}s, {fps:.0f} FPS). Đã tự động làm mới khung làn đường. Chấm 4 góc trên ảnh để giới hạn làn đường hoặc bấm <b>'🚀 BẮT ĐẦU DỰ ĐOÁN AI'</b>!</div>"
    return _render_traffic_base_with_roi(frame0, new_state), slider_update, new_state, status_msg


def on_traffic_image_loaded(img_rgb, state_data):
    """Nạp ảnh từ Tab Ảnh sang màn hình bên phải của Tab Đo Độ Ùn Tắc."""
    stop_live_stream()
    global_traffic_analyzer.reset()
    global_traffic_analyzer.clear_roi()
    if img_rgb is None:
        return gr.skip(), gr.skip(), state_data, "<div style='color:orange;'>Chưa có ảnh.</div>"

    frame_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    SERVER_FRAME_CACHE["traffic_base"] = frame_bgr
    new_state = {
        "source_type": "image",
        "video_path": state_data.get("video_path", "") if state_data else "",
        "cam_source": state_data.get("cam_source") if state_data else None,
        "cam_title": "Ảnh tĩnh tải lên",
        "total_frames": 1,
        "fps": 1.0,
        "current_frame_idx": 0,
        "frame_key": "traffic_base",
        "draft_roi_pts": [],
    }
    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video", interactive=False)
    msg = "<div style='color:#38bdf8;'>🖼️ <b>Ảnh đã sẵn sàng!</b> (Đã tự động làm mới khung làn đường). Chấm 4 góc để chọn làn đường hoặc bấm <b>'🚀 BẮT ĐẦU DỰ ĐOÁN AI'</b>.</div>"
    return _render_traffic_base_with_roi(frame_bgr, new_state), slider_update, new_state, msg


def on_traffic_camera_connect(cam_url_str, state_data):
    """Ngắt luồng đang chạy và kết nối lại với Camera RTSP / Live Stream trong Tab Đo Độ Ùn Tắc."""
    stop_live_stream()
    global_traffic_analyzer.reset()
    global_traffic_analyzer.clear_roi()
    raw_src = str(cam_url_str).strip() if cam_url_str is not None else CANTHO_CAMERAS[0]["url"]
    cam_src = resolve_camera_stream_url(raw_src)

    cam_info = next((c for c in CANTHO_CAMERAS if c["url"] == raw_src), None)
    cam_title = f"{cam_info['name']} ({cam_info['address']})" if cam_info else raw_src

    cap = cv2.VideoCapture(cam_src)
    ret, frame_bgr = (False, None)
    if cap.isOpened():
        for _ in range(3):
            ret, frame_bgr = cap.read()
            if ret and frame_bgr is not None:
                break
    cap.release()

    if not ret or frame_bgr is None:
        if state_data:
            state_data["source_type"] = "camera_stream"
            state_data["cam_source"] = cam_src
            state_data["cam_title"] = cam_title
        return (
            gr.skip(),
            gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video", interactive=False),
            state_data,
            f"<div style='color:#ef4444;'>⚠️ Đã ngắt luồng cũ nhưng không thể lấy khung hình từ <b>{cam_title}</b>. Vui lòng kiểm tra kết nối mạng hoặc chọn Camera khác!</div>",
        )

    SERVER_FRAME_CACHE["traffic_base"] = frame_bgr
    new_state = {
        "source_type": "camera_stream",
        "video_path": state_data.get("video_path", "") if state_data else "",
        "cam_source": cam_src,
        "cam_title": cam_title,
        "total_frames": 1,
        "fps": 25.0,
        "current_frame_idx": 0,
        "frame_key": "traffic_base",
        "draft_roi_pts": [],
    }
    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video", interactive=False)
    msg = f"<div style='color:#22c55e;'>📷 <b>Đã kết nối {cam_title}!</b> Chấm 4 góc trên ảnh để chọn làn đường hoặc bấm <b>'🚀 BẮT ĐẦU DỰ ĐOÁN AI'</b>.</div>"
    return _render_traffic_base_with_roi(frame_bgr, new_state), slider_update, new_state, msg


def on_traffic_switch_to_image_tab(img_rgb, state_data):
    """Chuyển sang tab Ảnh trong Tab Đo Độ Ùn Tắc -> tự động ngắt ngay video / dự đoán AI đang chạy."""
    stop_live_stream()
    global_traffic_analyzer.reset()
    if img_rgb is not None:
        return on_traffic_image_loaded(img_rgb, state_data)
    if state_data:
        state_data["source_type"] = "image"
    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video", interactive=False)
    msg = "<div style='color:#38bdf8;'>🖼️ <b>Đã chuyển sang tab Ảnh</b> (đã tự động ngắt luồng đang chạy). Hãy tải ảnh lên để bắt đầu!</div>"
    return gr.skip(), slider_update, state_data, msg


def on_traffic_switch_to_video_tab(video_file, state_data):
    """Chuyển sang tab Video trong Tab Đo Độ Ùn Tắc -> tự động ngắt ngay dự đoán AI đang chạy."""
    stop_live_stream()
    global_traffic_analyzer.reset()
    if video_file:
        return on_traffic_video_loaded(video_file, state_data)
    video_path = (state_data.get("video_path") if state_data else None) or DEFAULT_GAUGE_VIDEO
    if video_path and Path(video_path).exists():
        return on_traffic_video_loaded(video_path, state_data)
    if state_data:
        state_data["source_type"] = "video"
    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video", interactive=True)
    msg = "<div style='color:#38bdf8;'>🎬 <b>Đã chuyển sang tab Video</b> (đã tự động ngắt luồng đang chạy). Hãy tải tệp video lên!</div>"
    return gr.skip(), slider_update, state_data, msg


def on_traffic_switch_to_camera_tab(cam_url_str, state_data):
    """Chuyển sang tab Camera trong Tab Đo Độ Ùn Tắc -> tự động ngắt ngay video / dự đoán AI đang chạy."""
    stop_live_stream()
    global_traffic_analyzer.reset()
    if state_data:
        state_data["source_type"] = "camera_stream"
    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video", interactive=False)
    msg = "<div style='color:#38bdf8;'>📷 <b>Đã chuyển sang tab Camera</b> (đã tự động ngắt video / dự đoán AI đang chạy). Chọn Camera và bấm <b>Kết nối</b>!</div>"
    return gr.skip(), slider_update, state_data, msg


def on_traffic_seek_frame(frame_idx, state_data):
    """Tua video đến khung hình frame_idx trên Tab Đo Độ Ùn Tắc."""
    stop_live_stream()
    global_traffic_analyzer.reset()
    if not state_data or not state_data.get("video_path"):
        return gr.skip(), state_data
    video_path = state_data["video_path"]
    frame = extract_video_frame(video_path, int(frame_idx))
    if frame is None:
        return gr.skip(), state_data
    set_state_base_frame(state_data, frame, "traffic_base")
    state_data["current_frame_idx"] = int(frame_idx)
    return _render_traffic_base_with_roi(frame, state_data), state_data


def on_traffic_canvas_click(evt: gr.SelectData, state_data, roi_label):
    """
    Xử lý nhấp chuột trên màn hình giám sát giao thông:
    - Chấm lần lượt 4 góc (Góc 1 -> Góc 2 -> Góc 3 -> Góc 4) để tạo khung 4 góc xác định làn đường.
    - Khi đã có đủ 4 góc: nhấp chuột vào đâu sẽ tự động kéo/dời góc gần nhất tới vị trí mới đó!
    """
    if not state_data or get_state_base_frame(state_data) is None:
        return gr.skip(), state_data, "<div style='color:orange;'>⚠️ Chưa có khung hình để vẽ làn đường!</div>"

    base_frame = get_state_base_frame(state_data)
    h, w = base_frame.shape[:2]
    x, y = int(evt.index[0]), int(evt.index[1])
    lbl = str(roi_label).strip() if (roi_label and str(roi_label).strip()) else "Làn đường giám sát"
    global_traffic_analyzer.roi_label = lbl

    # Trường hợp 1: Đã có sẵn khung 4 góc -> nhấp chuột sẽ kéo góc gần nhất tới vị trí mới (x, y)
    if global_traffic_analyzer.roi_points_norm and len(global_traffic_analyzer.roi_points_norm) == 4:
        pts_px = [
            (rx * w, ry * h)
            for rx, ry in global_traffic_analyzer.roi_points_norm
        ]
        nearest_idx = min(
            range(4),
            key=lambda i: math.hypot(pts_px[i][0] - x, pts_px[i][1] - y)
        )
        new_norm = list(global_traffic_analyzer.roi_points_norm)
        new_norm[nearest_idx] = (
            max(0.0, min(1.0, float(x) / max(1.0, float(w)))),
            max(0.0, min(1.0, float(y) / max(1.0, float(h)))),
        )
        global_traffic_analyzer.set_roi_norm(new_norm, label=lbl)
        state_data["draft_roi_pts"] = []
        msg = f"<div style='color:#22c55e;'>📐 Đã dời <b>Góc {nearest_idx + 1}</b> tới ({x}, {y}). Chỉ phương tiện đi vào trong khung 4 góc <b>'{lbl}'</b> mới được tính!</div>"
        return _render_traffic_base_with_roi(base_frame, state_data), state_data, msg

    # Trường hợp 2: Đang chấm lần lượt 4 góc (1 -> 2 -> 3 -> 4)
    draft_pts = list(state_data.get("draft_roi_pts", []))
    draft_pts.append((x, y))

    if len(draft_pts) < 4:
        state_data["draft_roi_pts"] = draft_pts
        rem = 4 - len(draft_pts)
        msg = f"<div style='color:#38bdf8;'>📍 Đã chấm <b>Góc {len(draft_pts)}/4</b> tại ({x}, {y}). Hãy chấm tiếp <b>{rem} góc nữa</b> để khép kín vùng làn đường!</div>"
        return _render_traffic_base_with_roi(base_frame, state_data), state_data, msg

    # Đã chấm đủ 4 góc -> chuẩn hóa [0..1] và kích hoạt vùng lọc làn đường
    norm_pts = [
        (
            max(0.0, min(1.0, float(px) / max(1.0, float(w)))),
            max(0.0, min(1.0, float(py) / max(1.0, float(h)))),
        )
        for px, py in draft_pts[:4]
    ]
    global_traffic_analyzer.set_roi_norm(norm_pts, label=lbl)
    state_data["draft_roi_pts"] = []
    msg = f"<div style='color:#22c55e;'>✅ <b>Đã thiết lập khung 4 góc làn đường '{lbl}'!</b> Nhấp vào gần góc bất kỳ nếu muốn kéo chỉnh lại góc đó, hoặc bấm <b>'🚀 BẮT ĐẦU DỰ ĐOÁN AI'</b>.</div>"
    return _render_traffic_base_with_roi(base_frame, state_data), state_data, msg


def on_traffic_roi_label_change(roi_label, state_data):
    """Cập nhật tên nhãn hiển thị trên khung 4 góc làn đường."""
    lbl = str(roi_label).strip() if (roi_label and str(roi_label).strip()) else "Làn đường giám sát"
    global_traffic_analyzer.roi_label = lbl
    if not state_data or get_state_base_frame(state_data) is None:
        return gr.skip()
    return _render_traffic_base_with_roi(get_state_base_frame(state_data), state_data)


def on_traffic_clear_roi(state_data):
    """Xóa khung 4 góc làn đường để quay về chế độ giám sát toàn khung hình."""
    global_traffic_analyzer.clear_roi()
    if state_data:
        state_data["draft_roi_pts"] = []
    base_frame = get_state_base_frame(state_data)
    if base_frame is None:
        return gr.skip(), state_data, "<div style='color:#f59e0b;'>🗑️ Đã xóa khung làn đường.</div>"
    msg = "<div style='color:#f59e0b;'>🗑️ <b>Đã xóa khung 4 góc làn đường</b> (quay về giám sát toàn khung hình). Hãy chấm 4 góc trên ảnh nếu muốn tạo khung mới!</div>"
    return _render_traffic_base_with_roi(base_frame, state_data), state_data, msg



def on_traffic_veh_conf_changed(new_conf, state_data):
    """Xử lý khi người dùng kéo thanh trượt ngưỡng phương tiện (traffic_veh_conf)."""
    val = float(new_conf) if new_conf is not None else 0.25
    LIVE_STREAM_STATE["veh_conf"] = val

    # Nếu đang phát trực tiếp video hoặc camera: luồng live sẽ tự động dùng ngưỡng mới ở frame kế tiếp
    if LIVE_STREAM_STATE.get("active", False):
        status = f"<div style='color:#38bdf8;'>🎯 <b>Đã cập nhật ngưỡng phương tiện: {val:.2f}</b> (áp dụng ngay vào luồng phát trực tiếp)</div>"
        return gr.skip(), gr.skip(), gr.skip(), status

    # Nếu đang xem ảnh tĩnh hoặc video đã dừng/tua:
    base_frame = get_state_base_frame(state_data)
    if base_frame is None:
        status = f"<div style='color:#38bdf8;'>🎯 <b>Đã thiết lập ngưỡng phương tiện: {val:.2f}</b></div>"
        return gr.skip(), gr.skip(), gr.skip(), status

    loc_name = state_data.get("cam_title", "Camera Giao Thông") if state_data else "Camera Giao Thông"
    try:
        hud_rgb, df, alarm_html, report = global_traffic_analyzer.analyze_frame(
            base_frame, vehicle_model, veh_conf=val, fps=1.0, is_single_image=True, location_name=loc_name
        )
        status_info = f"<div style='color:#22c55e;'>🎯 <b>Đã cập nhật ngưỡng phương tiện: {val:.2f}</b> | Trạng thái: <b>{report.status_label}</b> | Số xe: <b>{report.vehicle_count} xe</b></div>"
        return hud_rgb, df, alarm_html, status_info
    except Exception as e:
        return gr.skip(), gr.skip(), gr.skip(), f"<div style='color:red;'>⚠️ Lỗi cập nhật ngưỡng: {e}</div>"


@spaces.GPU(duration=120)
def run_traffic_congestion_live(state_data, veh_conf, start_frame_slider):
    """Chạy suy luận trực tiếp đánh giá độ ùn tắc giao thông (YOLO + ByteTrack) trên Ảnh, Video hoặc Camera."""
    LIVE_STREAM_STATE["run_id"] += 1
    my_run_id = LIVE_STREAM_STATE["run_id"]
    LIVE_STREAM_STATE["active"] = True
    LIVE_STREAM_STATE["veh_conf"] = float(veh_conf) if veh_conf is not None else 0.25
    global_traffic_analyzer.reset()

    source_type = state_data.get("source_type", "video") if state_data else "video"
    loc_name = state_data.get("cam_title", "Camera Giao Thông") if state_data else "Camera Giao Thông"

    # Trường hợp 1: Nguồn Ảnh tĩnh
    if source_type == "image":
        base_frame = get_state_base_frame(state_data)
        if base_frame is None:
            LIVE_STREAM_STATE["active"] = False
            yield None, pd.DataFrame(), "<div style='color:red;'>⚠️ Vui lòng tải ảnh lên trước khi dự đoán!</div>", "<div style='color:red;'>⚠️ Chưa có ảnh hợp lệ</div>"
            return

        cur_conf = float(LIVE_STREAM_STATE.get("veh_conf", veh_conf))
        hud_rgb, df, alarm_html, report = global_traffic_analyzer.analyze_frame(
            base_frame, vehicle_model, veh_conf=cur_conf, fps=1.0, is_single_image=True, location_name=loc_name
        )
        LIVE_STREAM_STATE["active"] = False
        status_info = f"<div style='color:#22c55e;'>✅ <b>Đã phân tích ảnh hoàn tất!</b> | Trạng thái: <b>{report.status_label}</b> | Số xe: <b>{report.vehicle_count} xe</b></div>"
        yield frame_to_b64(hud_rgb), df, alarm_html, status_info
        return

    # Trường hợp 2: Luồng Camera RTSP / Live Stream
    if source_type == "camera_stream":
        cam_src = state_data.get("cam_source", 0) if state_data else 0
        grabber = LatestFrameGrabber(cam_src)
        if not grabber.is_opened:
            grabber.release()
            base_frame = get_state_base_frame(state_data)
            if base_frame is not None and LIVE_STREAM_STATE["active"] and LIVE_STREAM_STATE["run_id"] == my_run_id:
                cur_conf = float(LIVE_STREAM_STATE.get("veh_conf", veh_conf))
                hud_rgb, df, alarm_html, report = global_traffic_analyzer.analyze_frame(
                    base_frame, vehicle_model, veh_conf=cur_conf, fps=25.0, is_single_image=True, location_name=loc_name
                )
                LIVE_STREAM_STATE["active"] = False
                yield hud_rgb, df, alarm_html, f"<div style='color:#22c55e;'>✅ Đã phân tích khung hình Camera | Trạng thái: <b>{report.status_label}</b></div>"
            else:
                LIVE_STREAM_STATE["active"] = False
                yield None, pd.DataFrame(), "<div style='color:red;'>⚠️ Không thể mở luồng Camera!</div>", "<div style='color:red;'>Lỗi kết nối Camera</div>"
            return

        last_seq = -1
        render_cnt = 0
        last_hud = None
        last_df = pd.DataFrame()
        last_alarm = ""
        try:
            while grabber.is_opened and LIVE_STREAM_STATE["active"] and LIVE_STREAM_STATE["run_id"] == my_run_id:
                ok, frame, frame_ts, seq = grabber.read_latest()
                if not ok or frame is None or seq == last_seq:
                    time.sleep(0.008)
                    continue
                last_seq = seq
                if not LIVE_STREAM_STATE["active"] or LIVE_STREAM_STATE["run_id"] != my_run_id:
                    break
                render_cnt += 1
                try:
                    cur_conf = float(LIVE_STREAM_STATE.get("veh_conf", veh_conf))
                    hud_rgb, df, alarm_html, report = global_traffic_analyzer.analyze_frame(
                        frame, vehicle_model, veh_conf=cur_conf, fps=grabber.fps,
                        is_single_image=False, location_name=loc_name, sim_time_s=frame_ts
                    )
                    if not LIVE_STREAM_STATE["active"] or LIVE_STREAM_STATE["run_id"] != my_run_id:
                        break
                    status_info = f"<div style='color:#38bdf8;'>📷 <b>Camera Live (Frame {seq})</b> | Trạng thái: <b>{report.status_label}</b> | Số xe: <b>{report.vehicle_count}</b> | Tốc độ TB: <b>{report.avg_speed_kmh:.1f} km/h</b></div>"
                    last_hud, last_df, last_alarm = hud_rgb, df, alarm_html
                    yield frame_to_b64(hud_rgb), df, alarm_html, status_info
                except Exception as frame_err:
                    print(f"⚠️ Lỗi xử lý Camera Giao thông Frame {seq}: {frame_err}")
                    continue
        except GeneratorExit:
            LIVE_STREAM_STATE["active"] = False
            return
        finally:
            grabber.release()
            if LIVE_STREAM_STATE.get("active", False) and LIVE_STREAM_STATE["run_id"] == my_run_id:
                LIVE_STREAM_STATE["active"] = False
                try:
                    if last_hud is not None:
                        yield frame_to_b64(last_hud), last_df, last_alarm, "<div style='color:#22c55e;'>✅ <b>Đã ngắt luồng Camera giao thông!</b></div>"
                except GeneratorExit:
                    return
        return

    # Trường hợp 3: Nguồn Video MP4
    video_raw = state_data.get("video_path") if state_data else None
    if not video_raw and DEFAULT_GAUGE_VIDEO:
        video_raw = DEFAULT_GAUGE_VIDEO

    video_path = get_clean_filepath(video_raw)
    if not video_path or not Path(video_path).exists():
        LIVE_STREAM_STATE["active"] = False
        yield frame_to_html_img(None), pd.DataFrame(), "<div style='color:red;'>⚠️ Vui lòng tải video lên trước khi nhận diện!</div>", "<div style='color:red;'>⚠️ Chưa có video hợp lệ</div>"
        return

    LIVE_STREAM_STATE["video_path"] = video_path
    LIVE_STREAM_STATE["active"] = True
    LIVE_STREAM_STATE["veh_conf"] = float(veh_conf) if veh_conf is not None else 0.25

    # Kích hoạt luồng MJPEG Traffic Streaming chuẩn 25-30 FPS
    cur_conf = float(LIVE_STREAM_STATE.get("veh_conf", 0.25))
    mjpeg_url = f"/api/v1/stream/traffic?veh_conf={cur_conf}&t={int(time.time()*1000)}"
    stream_html = frame_to_html_img(stream_url=mjpeg_url)
    status_info = "<div style='color:#22c55e;'>🟢 <b>Đang phát luồng Giám Sát Giao Thông Realtime (25–30 FPS)</b> | YOLO + ByteTrack</div>"
    yield stream_html, pd.DataFrame(), "<div style='color:#22c55e; font-weight:bold;'>🚗 Đang theo dõi phương tiện realtime 25–30 FPS...</div>", status_info
    return

# ==============================================================================
# HỆ THỐNG TÍCH HỢP (DUAL FUSION): ĐO ĐỘ SÂU NGẬP & GIÁM SÁT ÙN TẮC (TUẦN TỰ 2 MODEL)
# ==============================================================================
def on_fusion_switch_to_image_tab(img_rgb, state_data):
    stop_live_stream()
    global_traffic_analyzer.reset()
    if img_rgb is not None:
        return on_fusion_image_loaded(img_rgb, state_data)
    if state_data:
        state_data["source_type"] = "image"
    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video", interactive=False)
    msg = "<div style='color:#38bdf8;'>🖼️ <b>Đã chuyển sang tab Ảnh Tích Hợp</b>. Tải ảnh lên và nhấn bắt đầu để chạy tuần tự 2 Model!</div>"
    return gr.skip(), slider_update, state_data, msg

def on_fusion_switch_to_video_tab(video_file, state_data):
    stop_live_stream()
    global_traffic_analyzer.reset()
    if video_file is not None:
        return on_fusion_video_loaded(video_file, state_data)
    if state_data:
        state_data["source_type"] = "video"
        state_data["video_path"] = DEFAULT_GAUGE_VIDEO
    slider_update = gr.Slider(minimum=0, maximum=100, value=0, step=1, label="Khung hình video", interactive=True)
    msg = "<div style='color:#38bdf8;'>🎬 <b>Đã chuyển sang tab Video Tích Hợp</b>. Sẵn sàng video mặc định hoặc tải video mới!</div>"
    return INIT_GAUGE_FRAME, slider_update, state_data, msg

def on_fusion_switch_to_camera_tab(cam_url, state_data):
    stop_live_stream()
    global_traffic_analyzer.reset()
    return on_fusion_camera_connect(cam_url, state_data)

def on_fusion_image_loaded(img_rgb, state_data):
    stop_live_stream()
    global_traffic_analyzer.reset()
    if img_rgb is None:
        return gr.skip(), gr.skip(), state_data, "<div style='color:red;'>⚠️ Chưa có ảnh tải lên!</div>"
    if state_data is None:
        state_data = DEFAULT_FUSION_STATE.copy()
    state_data["source_type"] = "image"
    set_state_base_frame(state_data, img_rgb.copy(), "fusion_base")
    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình video", interactive=False)
    msg = "<div style='color:#22c55e;'>✅ <b>Đã nạp ảnh thành công!</b> Nhấn <b>BẮT ĐẦU DỰ ĐOÁN AI</b> để phân tích tuần tự cả Ngập Lụt & Phương Tiện.</div>"
    return img_rgb, slider_update, state_data, msg

def on_fusion_video_loaded(video_file, state_data):
    stop_live_stream()
    global_traffic_analyzer.reset()
    clean_path = get_clean_filepath(video_file)
    if not clean_path or not Path(clean_path).exists():
        clean_path = DEFAULT_GAUGE_VIDEO
    if state_data is None:
        state_data = DEFAULT_FUSION_STATE.copy()
    state_data["source_type"] = "video"
    state_data["video_path"] = clean_path

    cap = cv2.VideoCapture(clean_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 100
    ret, frame_bgr = cap.read()
    cap.release()

    first_frame = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB) if ret and frame_bgr is not None else INIT_GAUGE_FRAME
    set_state_base_frame(state_data, first_frame, "fusion_base")
    state_data["total_frames"] = total_frames
    slider_update = gr.Slider(minimum=0, maximum=max(1, total_frames - 1), value=0, step=1, label="Khung hình video", interactive=True)
    msg = f"<div style='color:#22c55e;'>✅ <b>Đã tải video:</b> {Path(clean_path).name} ({total_frames} frames). Sẵn sàng phân tích tích hợp tuần tự 2 Model!</div>"
    return first_frame, slider_update, state_data, msg

def on_fusion_camera_connect(cam_url, state_data):
    stop_live_stream()
    global_traffic_analyzer.reset()
    if state_data is None:
        state_data = DEFAULT_FUSION_STATE.copy()
    cam_name = "Camera Trực Tiếp"
    for c in CANTHO_CAMERAS:
        if c["url"] == cam_url:
            cam_name = f"{c['name']} ({c['short_loc']})"
            break
    state_data["source_type"] = "camera_stream"
    state_data["cam_source"] = cam_url
    state_data["cam_title"] = cam_name

    cap = cv2.VideoCapture(cam_url)
    ret, frame_bgr = cap.read()
    cap.release()
    first_frame = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB) if ret and frame_bgr is not None else INIT_GAUGE_FRAME
    set_state_base_frame(state_data, first_frame, "fusion_base")
    slider_update = gr.Slider(minimum=0, maximum=1, value=0, step=1, label="Khung hình Camera", interactive=False)
    msg = f"<div style='color:#22c55e;'>📷 <b>Đã kết nối {cam_name}!</b> Bấm <b>BẮT ĐẦU DỰ ĐOÁN AI</b> để phân tích ngập & giao thông trực tiếp.</div>"
    return first_frame, slider_update, state_data, msg

def on_fusion_seek_frame(slider_val, state_data):
    if not state_data or state_data.get("source_type") != "video":
        return gr.skip(), state_data
    vid_path = state_data.get("video_path")
    if not vid_path:
        return gr.skip(), state_data
    cap = cv2.VideoCapture(vid_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(slider_val))
    ret, frame_bgr = cap.read()
    cap.release()
    if ret and frame_bgr is not None:
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        set_state_base_frame(state_data, frame_rgb, "fusion_base")
        state_data["current_frame_idx"] = int(slider_val)
        return frame_rgb, state_data
    return gr.skip(), state_data


def run_fusion_live(state_data, flood_conf, veh_conf, start_frame_slider):
    """
    Chạy suy luận TUẦN TỰ 2 Model (Model 1: Ngập Lụt -> Model 2: Phương Tiện & Ùn Tắc)
    trên từng khung hình của Ảnh, Video hoặc Camera Streaming.
    """
    LIVE_STREAM_STATE["run_id"] += 1
    my_run_id = LIVE_STREAM_STATE["run_id"]
    LIVE_STREAM_STATE["active"] = True
    LIVE_STREAM_STATE["flood_conf"] = float(flood_conf) if flood_conf is not None else 0.25
    LIVE_STREAM_STATE["veh_conf"] = float(veh_conf) if veh_conf is not None else 0.25
    global_traffic_analyzer.reset()

    active_flood = get_selected_flood_model()
    source_type = state_data.get("source_type", "video") if state_data else "video"
    loc_name = state_data.get("cam_title", "Camera Tích Hợp") if state_data else "Camera Tích Hợp"

    def build_fusion_outputs(flood_rep, traffic_rep):
        depth = flood_rep.master_depth_cm if flood_rep else 0.0
        depth_str = f"{depth:.1f} cm"
        flood_level = "AN TOÀN"
        flood_color = "#22c55e"
        if depth >= 50:
            flood_level = "CỰC KỲ NGUY HIỂM"
            flood_color = "#ef4444"
        elif depth >= 30:
            flood_level = "BÁO ĐỘNG CẤP 2 (NGẬP SÂU)"
            flood_color = "#f97316"
        elif depth >= 15:
            flood_level = "BÁO ĐỘNG CẤP 1 (NGẬP NHẸ)"
            flood_color = "#eab308"

        flood_alarm = f"<span style='color:{flood_color}; font-weight:700;'>{flood_level} ({depth_str})</span>"
        veh_count = traffic_rep.vehicle_count if traffic_rep else 0
        status_label = traffic_rep.status_label if traffic_rep else "Bình thường"
        avg_speed = traffic_rep.avg_speed_kmh if traffic_rep else 0.0

        alert_html = f"""
        <div style='background:rgba(15,23,42,0.95); border:1px solid #334155; border-radius:12px; padding:16px; margin-bottom:12px;'>
            <div style='display:flex; justify-content:space-between; align-items:center;'>
                <div>
                    <span style='font-size:1.1rem; font-weight:700; color:#38bdf8;'>🌊 Mực Nước: </span>
                    <span style='font-size:1.3rem; font-weight:800; color:{flood_color};'>{depth_str}</span>
                    <span style='margin-left:8px; font-size:0.9rem; padding:3px 8px; border-radius:6px; background:rgba(255,255,255,0.1); color:{flood_color};'>{flood_level}</span>
                </div>
                <div>
                    <span style='font-size:1.1rem; font-weight:700; color:#38bdf8;'>🚗 Phương Tiện: </span>
                    <span style='font-size:1.3rem; font-weight:800; color:#f8fafc;'>{veh_count} xe</span>
                    <span style='margin-left:8px; font-size:0.9rem; padding:3px 8px; border-radius:6px; background:rgba(255,255,255,0.1); color:#38bdf8;'>{status_label}</span>
                </div>
            </div>
        </div>
        """

        summary_df = pd.DataFrame([
            ["Độ Sâu Ngập Nước", f"{depth:.1f} cm"],
            ["Cấp Báo Động Ngập", f"{flood_alarm}"],
            ["Số Lượng Phương Tiện", f"{veh_count} xe"],
            ["Trạng Thái Giao Thông", f"{status_label}"],
            ["Vận Tốc Trung Bình", f"{avg_speed:.1f} km/h"],
            ["Chế Độ Thực Thi AI", "Tuần tự 2 Model (Tesla T4 GPU)"]
        ], columns=["Chỉ Số Giám Sát", "Giá Trị"])

        return summary_df, alert_html

    # Trường hợp 1: Ảnh tĩnh
    if source_type == "image":
        base_frame = get_state_base_frame(state_data)
        if base_frame is None:
            LIVE_STREAM_STATE["active"] = False
            yield None, pd.DataFrame(), "<div style='color:red;'>⚠️ Vui lòng tải ảnh lên trước khi dự đoán!</div>", "<div style='color:red;'>⚠️ Chưa có ảnh hợp lệ</div>"
            return

        cur_flood_conf = float(LIVE_STREAM_STATE.get("flood_conf", flood_conf))
        cur_veh_conf = float(LIVE_STREAM_STATE.get("veh_conf", veh_conf))

        flood_hud_rgb, flood_df, flood_alarm_html, flood_report = process_single_frame_hybrid(
            base_frame, global_multi_gauge_manager, global_tab5_arbiter,
            active_flood, None,
            flood_conf=cur_flood_conf, mode="Cột mốc"
        )
        flood_hud_bgr = cv2.cvtColor(flood_hud_rgb, cv2.COLOR_RGB2BGR)

        final_hud_rgb, traffic_df, traffic_alarm_html, traffic_report = global_traffic_analyzer.analyze_frame(
            flood_hud_bgr, vehicle_model, veh_conf=cur_veh_conf, fps=1.0, is_single_image=True, location_name=loc_name
        )
        LIVE_STREAM_STATE["active"] = False
        summary_df, alert_html = build_fusion_outputs(flood_report, traffic_report)
        status_info = f"<div style='color:#22c55e;'>✅ <b>Phân tích hoàn tất!</b> Mực nước: <b>{flood_report.master_depth_cm:.1f} cm</b> | Phương tiện: <b>{traffic_report.vehicle_count} xe</b> ({traffic_report.status_label})</div>"
        yield frame_to_b64(final_hud_rgb), summary_df, alert_html, status_info
        return

    # Trường hợp 2: Camera RTSP / Streaming
    if source_type == "camera_stream":
        cam_src = state_data.get("cam_source", 0) if state_data else 0
        grabber = LatestFrameGrabber(cam_src)
        if not grabber.is_opened:
            grabber.release()
            LIVE_STREAM_STATE["active"] = False
            yield None, pd.DataFrame(), "<div style='color:red;'>⚠️ Không thể mở luồng Camera!</div>", "<div style='color:red;'>Lỗi kết nối Camera</div>"
            return

        last_seq = -1
        render_cnt = 0
        last_hud = None
        last_df = pd.DataFrame()
        last_alert = ""
        try:
            while grabber.is_opened and LIVE_STREAM_STATE["active"] and LIVE_STREAM_STATE["run_id"] == my_run_id:
                ok, frame, frame_ts, seq = grabber.read_latest()
                if not ok or frame is None or seq == last_seq:
                    time.sleep(0.008)
                    continue
                last_seq = seq
                if not LIVE_STREAM_STATE["active"] or LIVE_STREAM_STATE["run_id"] != my_run_id:
                    break
                render_cnt += 1
                try:
                    cur_flood_conf = float(LIVE_STREAM_STATE.get("flood_conf", flood_conf))
                    cur_veh_conf = float(LIVE_STREAM_STATE.get("veh_conf", veh_conf))

                    flood_hud_rgb, flood_df, flood_alarm_html, flood_report = process_single_frame_hybrid(
                        frame, global_multi_gauge_manager, global_tab5_arbiter,
                        active_flood, None,
                        flood_conf=cur_flood_conf, mode="Cột mốc"
                    )
                    if not LIVE_STREAM_STATE["active"] or LIVE_STREAM_STATE["run_id"] != my_run_id:
                        break

                    flood_hud_bgr = cv2.cvtColor(flood_hud_rgb, cv2.COLOR_RGB2BGR)

                    final_hud_rgb, traffic_df, traffic_alarm_html, traffic_report = global_traffic_analyzer.analyze_frame(
                        flood_hud_bgr, vehicle_model, veh_conf=cur_veh_conf, fps=grabber.fps,
                        is_single_image=False, location_name=loc_name, sim_time_s=frame_ts
                    )
                    if not LIVE_STREAM_STATE["active"] or LIVE_STREAM_STATE["run_id"] != my_run_id:
                        break

                    summary_df, alert_html = build_fusion_outputs(flood_report, traffic_report)
                    status_info = f"<div style='color:#38bdf8;'>📷 <b>Camera Live (Frame {seq})</b> | Mực nước: <b>{flood_report.master_depth_cm:.1f} cm</b> | Xe: <b>{traffic_report.vehicle_count}</b> ({traffic_report.status_label})</div>"
                    last_hud, last_df, last_alert = final_hud_rgb, summary_df, alert_html
                    yield frame_to_b64(final_hud_rgb), summary_df, alert_html, status_info
                except Exception as frame_err:
                    print(f"⚠️ Lỗi xử lý Camera Fusion Frame {seq}: {frame_err}")
                    continue
        except GeneratorExit:
            LIVE_STREAM_STATE["active"] = False
            return
        finally:
            grabber.release()
            if LIVE_STREAM_STATE.get("active", False) and LIVE_STREAM_STATE["run_id"] == my_run_id:
                LIVE_STREAM_STATE["active"] = False
                try:
                    if last_hud is not None:
                        yield frame_to_b64(last_hud), last_df, last_alert, "<div style='color:#22c55e;'>✅ <b>Đã ngắt luồng Camera tích hợp!</b></div>"
                except GeneratorExit:
                    return
        return

    # Trường hợp 3: Video MP4
    video_raw = state_data.get("video_path") if state_data else None
    if not video_raw and DEFAULT_GAUGE_VIDEO:
        video_raw = DEFAULT_GAUGE_VIDEO

    video_path = get_clean_filepath(video_raw)
    if not video_path or not Path(video_path).exists():
        LIVE_STREAM_STATE["active"] = False
        yield frame_to_html_img(None), pd.DataFrame(), "<div style='color:red;'>⚠️ Vui lòng tải video lên trước khi nhận diện!</div>", "<div style='color:red;'>⚠️ Chưa có video hợp lệ</div>"
        return

    LIVE_STREAM_STATE["video_path"] = video_path
    LIVE_STREAM_STATE["active"] = True
    LIVE_STREAM_STATE["flood_conf"] = float(flood_conf) if flood_conf is not None else 0.25
    LIVE_STREAM_STATE["veh_conf"] = float(veh_conf) if veh_conf is not None else 0.25

    # Kích hoạt luồng MJPEG Fusion Streaming chuẩn 25-30 FPS
    cur_flood_conf = float(LIVE_STREAM_STATE.get("flood_conf", 0.25))
    cur_veh_conf = float(LIVE_STREAM_STATE.get("veh_conf", 0.25))
    mjpeg_url = f"/api/v1/stream/fusion?flood_conf={cur_flood_conf}&veh_conf={cur_veh_conf}&t={int(time.time()*1000)}"
    stream_html = frame_to_html_img(stream_url=mjpeg_url)
    status_info = "<div style='color:#22c55e;'>🟢 <b>Đang phát luồng Phân Tích Kép Realtime (25–30 FPS)</b> | Thủy Văn + Giao Thông</div>"
    yield stream_html, pd.DataFrame(), "<div style='color:#22c55e; font-weight:bold;'>🌊🚗 Đang phân tích kép realtime 25–30 FPS...</div>", status_info
    return

# ==============================================================================
# GIAO DIỆN GRADIO HIỆN ĐẠI
# ==============================================================================
custom_css = """
.gradio-container {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}
.header-box {
    background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
    color: white;
    padding: 22px 28px;
    border-radius: 14px;
    margin-bottom: 16px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.12);
}
#tab5_calib_canvas, #traffic_canvas {
    margin-top: 0px !important;
    padding-top: 0px !important;
}
#tab5_setup_status:empty, #traffic_setup_status:empty {
    display: none !important;
}
"""

with gr.Blocks(title="AI Urban Flood & Traffic Congestion Monitor", theme=gr.themes.Soft(), css=custom_css) as demo:
    gr.HTML("""
    <div class="header-box">
        <h1 style="margin: 0; font-size: 26px; font-weight: 700;">
            🌊 AI Urban Flood & Traffic Congestion Monitor
        </h1>
        <p style="margin: 8px 0 0 0; font-size: 15px; opacity: 0.95;">
            Hệ thống giám sát thông minh thời gian thực: <b>Đo Độ Sâu Ngập Theo Thủy Chuẩn Cột Mốc (SCSG-Net)</b> & <b>Đo Độ Ùn Tắc Giao Thông Đa Chiều (YOLO + ByteTrack)</b>.
        </p>
    </div>
    """)

    with gr.Tabs() as main_tabs:
        # ======================================================================
        # TAB 1: ⚡ HỆ THỐNG ĐO ĐỘ SÂU NGẬP THEO THỦY CHUẨN CỘT MỐC
        # ======================================================================
        with gr.TabItem("⚡ Hệ Thống Đo Độ Sâu Ngập Theo Thủy Chuẩn Cột Mốc", id="tab5") as tab_item_5:
            with gr.Row():
                # CỘT BÊN TRÁI: NGUỒN ĐẦU VÀO (ẢNH / VIDEO / CAMERA) & ĐIỀU KHIỂN DỰ ĐOÁN AI
                with gr.Column(scale=2):
                    gr.Markdown("#### 📁 1. Nguồn Đầu Vào & Điều Khiển Dự Đoán AI")
                    with gr.Tabs(selected="input_video") as tab5_input_tabs:
                        with gr.TabItem("Ảnh", id="input_image") as tab_input_image:
                            tab5_upload_image = gr.Image(
                                sources=["upload", "clipboard"],
                                type="numpy",
                                label="Tải ảnh",
                                interactive=True
                            )
                        with gr.TabItem("Video", id="input_video") as tab_input_video:
                            tab5_upload_video = gr.Video(
                                value=DEFAULT_GAUGE_VIDEO,
                                label="Tải video",
                                interactive=True
                            )
                        with gr.TabItem("Camera", id="input_camera") as tab_input_camera:
                            with gr.Row():
                                tab5_cam_preset = gr.Dropdown(
                                    choices=[(f"{c['name']} - {c['short_loc']}", c["url"]) for c in CANTHO_CAMERAS],
                                    value=CANTHO_CAMERAS[0]["url"],
                                    label="Chọn Camera",
                                    interactive=True,
                                    scale=3
                                )
                                btn_tab5_connect_cam = gr.Button("Kết nối", variant="primary", scale=1)

                    with gr.Row():
                        btn_tab5_run_live = gr.Button("🚀 BẮT ĐẦU DỰ ĐOÁN AI", variant="primary", size="lg")
                        btn_tab5_stop_live = gr.Button("⏹️ Dừng Lại / Tạm Dừng", variant="stop")

                    tab5_alarm_html = gr.HTML("")

                    tab5_stat_table = gr.DataFrame(
                        headers=["Nguồn Đo Lường", "Độ Sâu Ngập", "Cấp Báo Động", "Độ Tin Cậy", "Thủy Chuẩn Mốc"],
                        datatype=["str", "str", "str", "str", "str"],
                        label="📋 Thông Số Thủy Văn & Thủy Chuẩn Cột Mốc"
                    )

                    with gr.Accordion("⚙️ Cài Đặt Nâng Cao & Danh Sách Cột Mốc Đã Lưu", open=False):
                        with gr.Row():
                            tab5_active_dropdown = gr.Dropdown(
                                choices=get_gauge_dropdown_choices(),
                                value=None,
                                label="📍 Cột mốc đã lưu",
                                interactive=True,
                                scale=3
                            )
                            btn_tab5_delete_gauge = gr.Button("🗑️ Xóa cột mốc", variant="stop", scale=1)

                        tab5_flood_conf = gr.Slider(0.10, 0.90, value=0.25, step=0.05, label="🎯 Ngưỡng Nước (Flood Conf)")

                # CỘT BÊN PHẢI: HIỂN THỊ KHUNG HÌNH VIDEO & THIẾT LẬP MẪU MỐC THƯỚC ĐO
                with gr.Column(scale=3):
                    gr.Markdown("#### 🖼️ 2. Màn Hình Video & Thiết Lập Mẫu Mốc Thước Đo")
                    tab5_init_frame = None
                    if INIT_GAUGE_FRAME is not None:
                        tab5_init_prev = draw_multi_calibration_preview(
                            cv2.cvtColor(INIT_GAUGE_FRAME, cv2.COLOR_RGB2BGR),
                            global_multi_gauge_manager,
                            active_gauge_id=global_multi_gauge_manager.active_id
                        )
                        if tab5_init_prev is not None:
                            tab5_init_frame = cv2.cvtColor(tab5_init_prev, cv2.COLOR_BGR2RGB)
                    if tab5_init_frame is None:
                        tab5_init_frame = INIT_GAUGE_FRAME

                    tab5_video_display = gr.HTML(
                        value=frame_to_html_img(tab5_init_frame),
                        elem_id="tab5_video_display"
                    )
                    with gr.Accordion("📐 Thiết lập mốc thước đo (Click chấm điểm khi cần)", open=False):
                        tab5_calib_canvas = gr.Image(
                            value=tab5_init_frame,
                            type="numpy",
                            format="jpeg",
                            show_label=False,
                            interactive=True,
                            elem_id="tab5_calib_canvas"
                        )
                    tab5_setup_status = gr.HTML("", elem_id="tab5_setup_status")
                    tab5_click_state = gr.State(DEFAULT_TAB5_STATE.copy())
                    tab5_seek_slider = gr.Slider(
                        minimum=0,
                        maximum=max(1, INIT_TOTAL_FRAMES - 1),
                        value=0,
                        step=1,
                        label="Khung hình video (chọn mốc)",
                        interactive=True
                    )

                    gr.Markdown("##### Thông tin cột mốc")
                    tab5_name_input = gr.Textbox(
                        value="",
                        label="Tên cột mốc",
                        placeholder="Nhập tên cột mốc..."
                    )
                    with gr.Row():
                        tab5_h_start_input = gr.Number(value=0, label="Điểm đầu (cm)", precision=1)
                        tab5_h_end_input = gr.Number(value=0, label="Điểm cuối (cm)", precision=1)
                    tab5_strip_width_slider = gr.Slider(
                        minimum=4,
                        maximum=100,
                        value=20,
                        step=2,
                        label="Bề rộng cột mốc",
                        interactive=True
                    )
                    with gr.Row():
                        btn_save_tab5_gauge = gr.Button("Lưu", variant="primary", scale=1)
                        btn_reset_tab5_gauge = gr.Button("Hủy", variant="secondary", scale=1)
                        btn_reset_all_tab5_gauges = gr.Button("🗑️ Xóa tất cả mốc", variant="stop", scale=1)

        # ======================================================================
        # TAB 2: 🚗 HỆ THỐNG ĐO ĐỘ ÙN TẮC GIAO THÔNG (YOLO + BYTETRACK)
        # ======================================================================
        with gr.TabItem("🚗 Hệ Thống Đo Độ Ùn Tắc Giao Thông", id="tab_traffic") as tab_item_traffic:
            with gr.Row():
                # CỘT BÊN TRÁI: NGUỒN ĐẦU VÀO (ẢNH / VIDEO / CAMERA) & ĐIỀU KHIỂN DỰ ĐOÁN AI
                with gr.Column(scale=2):
                    gr.Markdown("#### 📁 1. Nguồn Đầu Vào & Điều Khiển Dự Đoán AI")
                    with gr.Tabs(selected="traffic_input_video") as traffic_input_tabs:
                        with gr.TabItem("Ảnh", id="traffic_input_image") as tab_traffic_image:
                            traffic_upload_image = gr.Image(
                                sources=["upload", "clipboard"],
                                type="numpy",
                                label="Tải ảnh",
                                interactive=True
                            )
                        with gr.TabItem("Video", id="traffic_input_video") as tab_traffic_video:
                            traffic_upload_video = gr.Video(
                                value=DEFAULT_GAUGE_VIDEO,
                                label="Tải video",
                                interactive=True
                            )
                        with gr.TabItem("Camera", id="traffic_input_camera") as tab_traffic_camera:
                            with gr.Row():
                                traffic_cam_preset = gr.Dropdown(
                                    choices=[(f"{c['name']} - {c['short_loc']}", c["url"]) for c in CANTHO_CAMERAS],
                                    value=CANTHO_CAMERAS[0]["url"],
                                    label="Chọn Camera",
                                    interactive=True,
                                    scale=3
                                )
                                btn_traffic_connect_cam = gr.Button("Kết nối", variant="primary", scale=1)

                    with gr.Row():
                        btn_traffic_run_live = gr.Button("🚀 BẮT ĐẦU DỰ ĐOÁN AI", variant="primary", size="lg")
                        btn_traffic_stop_live = gr.Button("⏹️ Dừng Lại / Tạm Dừng", variant="stop")

                    traffic_alarm_html = gr.HTML("")

                    traffic_stat_table = gr.DataFrame(
                        headers=["Trạng Thái", "Số Phương Tiện", "Tốc Độ TB", "Lưu Lượng", "Hàng Chờ", "Thời Gian Dừng"],
                        datatype=["str", "str", "str", "str", "str", "str"],
                        label="📋 Thông Số Giao Thông & Trạng Thái Ùn Tắc (YOLO + ByteTrack)"
                    )

                    with gr.Accordion("⚙️ Cài Đặt Nâng Cao", open=False):
                        traffic_veh_conf = gr.Slider(
                            0.10, 0.90, value=0.25, step=0.05,
                            label="🚗 Ngưỡng Phát Hiện Phương Tiện (Vehicle Conf)"
                        )

                # CỘT BÊN PHẢI: MÀN HÌNH VIDEO GIÁM SÁT GIAO THÔNG & VÙNG LÀN ĐƯỜNG 4 GÓC
                with gr.Column(scale=3):
                    gr.Markdown("#### 🖼️ 2. Màn Hình Giám Sát Giao Thông & Chọn Làn Đường (Chấm 4 Góc)")
                    traffic_video_display = gr.HTML(
                        value=frame_to_html_img(INIT_GAUGE_FRAME),
                        elem_id="traffic_video_display"
                    )
                    with gr.Accordion("📐 Khoanh vùng làn đường giám sát (Click chấm điểm khi cần)", open=False):
                        traffic_canvas = gr.Image(
                            value=INIT_GAUGE_FRAME,
                            type="numpy",
                            format="jpeg",
                            show_label=False,
                            interactive=True,
                            elem_id="traffic_canvas"
                        )
                    traffic_setup_status = gr.HTML(
                        "<div style='color:#38bdf8;'>💡 <b>Mẹo:</b> Nhấp <b>4 góc</b> trên màn hình để vẽ khung làn đường (chỉ tính xe đi vào khung). Khi đã đủ 4 góc, nhấp gần góc nào để kéo chỉnh lại góc đó!</div>",
                        elem_id="traffic_setup_status"
                    )
                    traffic_state = gr.State(DEFAULT_TRAFFIC_STATE.copy())
                    traffic_seek_slider = gr.Slider(
                        minimum=0,
                        maximum=max(1, INIT_TOTAL_FRAMES - 1),
                        value=0,
                        step=1,
                        label="Khung hình video",
                        interactive=True
                    )
                    with gr.Row():
                        traffic_roi_label = gr.Textbox(
                            value="Làn đường giám sát",
                            label="Nhãn khung làn đường (4 góc)",
                            placeholder="Nhập tên làn đường...",
                            scale=3
                        )
                        btn_traffic_clear_roi = gr.Button("🗑️ Xóa Khung Làn Đường", variant="secondary", scale=1)

        
        with gr.TabItem("🌊🚗 Tích Hợp: Đo Độ Sâu Ngập & Giám Sát Ùn Tắc (Dual Fusion)", id="tab_fusion") as tab_item_fusion:
            with gr.Row():
                # CỘT BÊN TRÁI: NGUỒN ĐẦU VÀO & BẢNG THÔNG SỐ TÍCH HỢP
                with gr.Column(scale=2):
                    gr.Markdown("#### 📁 1. Nguồn Đầu Vào & Điều Khiển 2 Model AI")
                    with gr.Tabs(selected="fusion_input_video") as fusion_input_tabs:
                        with gr.TabItem("Ảnh", id="fusion_input_image") as tab_fusion_image:
                            fusion_upload_image = gr.Image(
                                sources=["upload", "clipboard"],
                                type="numpy",
                                label="Tải ảnh",
                                interactive=True
                            )
                        with gr.TabItem("Video", id="fusion_input_video") as tab_fusion_video:
                            fusion_upload_video = gr.Video(
                                value=DEFAULT_GAUGE_VIDEO,
                                label="Tải video",
                                interactive=True
                            )
                        with gr.TabItem("Camera", id="fusion_input_camera") as tab_fusion_camera:
                            with gr.Row():
                                fusion_cam_preset = gr.Dropdown(
                                    choices=[(f"{c['name']} - {c['short_loc']}", c["url"]) for c in CANTHO_CAMERAS],
                                    value=CANTHO_CAMERAS[0]["url"],
                                    label="Chọn Camera",
                                    interactive=True,
                                    scale=3
                                )
                                btn_fusion_connect_cam = gr.Button("Kết nối", variant="primary", scale=1)

                    with gr.Row():
                        btn_fusion_run_live = gr.Button("🚀 BẮT ĐẦU DỰ ĐOÁN TÍCH HỢP (2 MODEL)", variant="primary", size="lg")
                        btn_fusion_stop_live = gr.Button("⏹️ Dừng Lại / Tạm Dừng", variant="stop")

                    fusion_alarm_html = gr.HTML("")

                    fusion_stat_table = gr.DataFrame(
                        headers=["Chỉ Số Giám Sát", "Giá Trị"],
                        datatype=["str", "str"],
                        label="📋 Bảng Chỉ Số Tích Hợp Thủy Văn & Giao Thông (Tuần Tự 2 Model)"
                    )

                    with gr.Accordion("⚙️ Cài Đặt Ngưỡng Tự Tin 2 Model", open=False):
                        fusion_flood_conf = gr.Slider(
                            0.10, 0.90, value=0.25, step=0.05,
                            label="🌊 Ngưỡng Nhận Diện Nước Ngập (Flood Conf)"
                        )
                        fusion_veh_conf = gr.Slider(
                            0.10, 0.90, value=0.25, step=0.05,
                            label="🚗 Ngưỡng Phát Hiện Phương Tiện (Vehicle Conf)"
                        )

                # CỘT BÊN PHẢI: MÀN HÌNH GIÁM SÁT TÍCH HỢP 2 TRONG 1
                with gr.Column(scale=3):
                    gr.Markdown("#### 🖼️ 2. Màn Hình Giám Sát Tích Hợp (Mực Nước + Phương Tiện Realtime)")
                    fusion_video_display = gr.HTML(
                        value=frame_to_html_img(INIT_GAUGE_FRAME),
                        elem_id="fusion_video_display"
                    )
                    fusion_canvas = gr.Image(
                        value=INIT_GAUGE_FRAME,
                        type="numpy",
                        format="jpeg",
                        show_label=False,
                        interactive=False,
                        visible=False,
                        elem_id="fusion_canvas"
                    )
                    fusion_setup_status = gr.HTML(
                        "<div style='color:#38bdf8;'>💡 <b>Chế độ tích hợp:</b> Hệ thống chạy tuần tự Model 1 (Phân đoạn ngập & Đo độ sâu thủy chuẩn) sau đó đưa frame vào Model 2 (YOLOv8 + ByteTrack) để vẽ đồng thời lớp ngập và phương tiện.</div>",
                        elem_id="fusion_setup_status"
                    )
                    fusion_seek_slider = gr.Slider(
                        minimum=0,
                        maximum=100,
                        value=0,
                        step=1,
                        label="Khung hình video",
                        interactive=True
                    )

# ==================== SỰ KIỆN CHUNG & TAB 1 (NGẬP LỤT) ====================
        tab5_live_event = btn_tab5_run_live.click(
            fn=run_video_tab5_live,
            inputs=[tab5_click_state, tab5_flood_conf, tab5_seek_slider],
            outputs=[tab5_video_display, tab5_stat_table, tab5_alarm_html, tab5_setup_status]
        )

        traffic_live_event = btn_traffic_run_live.click(
            fn=run_traffic_congestion_live,
            inputs=[traffic_state, traffic_veh_conf, traffic_seek_slider],
            outputs=[traffic_video_display, traffic_stat_table, traffic_alarm_html, traffic_setup_status]
        )

        
        fusion_state = gr.State(DEFAULT_FUSION_STATE.copy())

        fusion_live_event = btn_fusion_run_live.click(
            fn=run_fusion_live,
            inputs=[fusion_state, fusion_flood_conf, fusion_veh_conf, fusion_seek_slider],
            outputs=[fusion_video_display, fusion_stat_table, fusion_alarm_html, fusion_setup_status]
        )

        all_live_events = [tab5_live_event, traffic_live_event, fusion_live_event]

        btn_fusion_stop_live.click(
            fn=on_tab5_stop_button,
            inputs=None,
            outputs=[fusion_video_display, fusion_setup_status],
            cancels=all_live_events
        )

        tab_item_fusion.select(
            fn=on_tab5_stop_button,
            inputs=None,
            outputs=[fusion_video_display, fusion_setup_status],
            cancels=all_live_events
        )

        tab_fusion_image.select(
            fn=on_fusion_switch_to_image_tab,
            inputs=[fusion_upload_image, fusion_state],
            outputs=[fusion_canvas, fusion_seek_slider, fusion_state, fusion_setup_status],
            cancels=all_live_events
        )
        tab_fusion_video.select(
            fn=on_fusion_switch_to_video_tab,
            inputs=[fusion_upload_video, fusion_state],
            outputs=[fusion_canvas, fusion_seek_slider, fusion_state, fusion_setup_status],
            cancels=all_live_events
        )
        tab_fusion_camera.select(
            fn=on_fusion_switch_to_camera_tab,
            inputs=[fusion_cam_preset, fusion_state],
            outputs=[fusion_canvas, fusion_seek_slider, fusion_state, fusion_setup_status],
            cancels=all_live_events
        )
        fusion_upload_image.change(
            fn=on_fusion_image_loaded,
            inputs=[fusion_upload_image, fusion_state],
            outputs=[fusion_canvas, fusion_seek_slider, fusion_state, fusion_setup_status],
            cancels=all_live_events
        )
        fusion_upload_video.change(
            fn=on_fusion_video_loaded,
            inputs=[fusion_upload_video, fusion_state],
            outputs=[fusion_canvas, fusion_seek_slider, fusion_state, fusion_setup_status],
            cancels=all_live_events
        )
        fusion_cam_preset.change(
            fn=on_fusion_camera_connect,
            inputs=[fusion_cam_preset, fusion_state],
            outputs=[fusion_canvas, fusion_seek_slider, fusion_state, fusion_setup_status],
            cancels=all_live_events
        )
        btn_fusion_connect_cam.click(
            fn=on_fusion_camera_connect,
            inputs=[fusion_cam_preset, fusion_state],
            outputs=[fusion_canvas, fusion_seek_slider, fusion_state, fusion_setup_status],
            cancels=all_live_events
        )
        fusion_seek_slider.release(
            fn=on_fusion_seek_frame,
            inputs=[fusion_seek_slider, fusion_state],
            outputs=[fusion_canvas, fusion_state],
            cancels=all_live_events
        )


        # Nút Dừng ở cả 2 Tab
        btn_tab5_stop_live.click(
            fn=on_tab5_stop_button,
            inputs=None,
            outputs=[tab5_video_display, tab5_setup_status],
            cancels=all_live_events
        )
        btn_traffic_stop_live.click(
            fn=on_tab5_stop_button,
            inputs=None,
            outputs=[traffic_video_display, traffic_setup_status],
            cancels=all_live_events
        )

        # Khi chuyển qua lại giữa 2 Tab chính (Đo Độ Sâu Ngập <-> Đo Độ Ùn Tắc) -> tự động ngắt luồng đang chạy
        tab_item_5.select(
            fn=on_tab5_stop_button,
            inputs=None,
            outputs=[traffic_video_display, traffic_setup_status],
            cancels=all_live_events
        )
        tab_item_traffic.select(
            fn=on_tab5_stop_button,
            inputs=None,
            outputs=[tab5_video_display, tab5_setup_status],
            cancels=all_live_events
        )

        # Sự kiện chuyển 3 tab con Ảnh / Video / Camera ở Tab 1 (Ngập lụt)
        tab_input_image.select(
            fn=on_tab5_switch_to_image_tab,
            inputs=[tab5_upload_image, tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_seek_slider, tab5_click_state, tab5_setup_status, tab5_active_dropdown],
            cancels=all_live_events
        )
        tab_input_video.select(
            fn=on_tab5_switch_to_video_tab,
            inputs=[tab5_upload_video, tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_seek_slider, tab5_click_state, tab5_setup_status, tab5_active_dropdown],
            cancels=all_live_events
        )
        tab_input_camera.select(
            fn=on_tab5_switch_to_camera_tab,
            inputs=[tab5_cam_preset, tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_seek_slider, tab5_click_state, tab5_setup_status, tab5_active_dropdown],
            cancels=all_live_events
        )

        # Xóa mốc đã lưu tại Tab 1
        btn_tab5_delete_gauge.click(
            fn=on_tab5_remove_active_gauge,
            inputs=[tab5_active_dropdown, tab5_click_state],
            outputs=[tab5_active_dropdown, tab5_calib_canvas, tab5_click_state, tab5_setup_status]
        )

        # Kéo thả đổi bề rộng thước đo (Reactive Width Slider)
        tab5_strip_width_slider.change(
            fn=on_tab5_width_change,
            inputs=[tab5_strip_width_slider, tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input],
            outputs=[tab5_calib_canvas, tab5_click_state, tab5_setup_status]
        )

        # Khi tải Ảnh / chọn Camera / kết nối Camera / tải Video / tua Video ở Tab 1
        tab5_upload_image.change(
            fn=on_tab5_image_loaded,
            inputs=[tab5_upload_image, tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_seek_slider, tab5_click_state, tab5_setup_status, tab5_active_dropdown],
            cancels=all_live_events
        )
        tab5_cam_preset.change(
            fn=on_tab5_camera_connect,
            inputs=[tab5_cam_preset, tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_seek_slider, tab5_click_state, tab5_setup_status, tab5_active_dropdown],
            cancels=all_live_events
        )
        btn_tab5_connect_cam.click(
            fn=on_tab5_camera_connect,
            inputs=[tab5_cam_preset, tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_seek_slider, tab5_click_state, tab5_setup_status, tab5_active_dropdown],
            cancels=all_live_events
        )
        tab5_upload_video.change(
            fn=on_tab5_video_loaded,
            inputs=[tab5_upload_video, tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_seek_slider, tab5_click_state, tab5_setup_status, tab5_active_dropdown],
            cancels=all_live_events
        )
        tab5_seek_slider.release(
            fn=on_tab5_seek_frame,
            inputs=[tab5_seek_slider, tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_click_state],
            cancels=all_live_events
        )
        tab5_calib_canvas.select(
            fn=on_tab5_canvas_click,
            inputs=[tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_click_state, tab5_setup_status]
        )
        btn_save_tab5_gauge.click(
            fn=on_tab5_save_gauge,
            inputs=[tab5_click_state, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_click_state, tab5_setup_status, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_active_dropdown]
        )
        btn_reset_tab5_gauge.click(
            fn=reset_tab5_gauge_points,
            inputs=[tab5_click_state, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_click_state, tab5_setup_status, tab5_name_input, tab5_h_start_input, tab5_h_end_input]
        )
        btn_reset_all_tab5_gauges.click(
            fn=on_tab5_reset_all_gauges,
            inputs=[tab5_click_state, tab5_strip_width_slider],
            outputs=[tab5_calib_canvas, tab5_click_state, tab5_setup_status, tab5_name_input, tab5_h_start_input, tab5_h_end_input, tab5_active_dropdown]
        )
        tab5_flood_conf.release(
            fn=on_tab5_flood_conf_changed,
            inputs=[tab5_flood_conf, tab5_click_state],
            outputs=[tab5_video_display, tab5_stat_table, tab5_alarm_html, tab5_setup_status]
        )

        # ==================== SỰ KIỆN TAB 2 (ĐO ĐỘ ÙN TẮC GIAO THÔNG) ====================
        tab_traffic_image.select(
            fn=on_traffic_switch_to_image_tab,
            inputs=[traffic_upload_image, traffic_state],
            outputs=[traffic_canvas, traffic_seek_slider, traffic_state, traffic_setup_status],
            cancels=all_live_events
        )
        tab_traffic_video.select(
            fn=on_traffic_switch_to_video_tab,
            inputs=[traffic_upload_video, traffic_state],
            outputs=[traffic_canvas, traffic_seek_slider, traffic_state, traffic_setup_status],
            cancels=all_live_events
        )
        tab_traffic_camera.select(
            fn=on_traffic_switch_to_camera_tab,
            inputs=[traffic_cam_preset, traffic_state],
            outputs=[traffic_canvas, traffic_seek_slider, traffic_state, traffic_setup_status],
            cancels=all_live_events
        )
        traffic_upload_image.change(
            fn=on_traffic_image_loaded,
            inputs=[traffic_upload_image, traffic_state],
            outputs=[traffic_canvas, traffic_seek_slider, traffic_state, traffic_setup_status],
            cancels=all_live_events
        )
        traffic_upload_video.change(
            fn=on_traffic_video_loaded,
            inputs=[traffic_upload_video, traffic_state],
            outputs=[traffic_canvas, traffic_seek_slider, traffic_state, traffic_setup_status],
            cancels=all_live_events
        )
        traffic_cam_preset.change(
            fn=on_traffic_camera_connect,
            inputs=[traffic_cam_preset, traffic_state],
            outputs=[traffic_canvas, traffic_seek_slider, traffic_state, traffic_setup_status],
            cancels=all_live_events
        )
        btn_traffic_connect_cam.click(
            fn=on_traffic_camera_connect,
            inputs=[traffic_cam_preset, traffic_state],
            outputs=[traffic_canvas, traffic_seek_slider, traffic_state, traffic_setup_status],
            cancels=all_live_events
        )
        traffic_seek_slider.release(
            fn=on_traffic_seek_frame,
            inputs=[traffic_seek_slider, traffic_state],
            outputs=[traffic_canvas, traffic_state],
            cancels=all_live_events
        )
        traffic_canvas.select(
            fn=on_traffic_canvas_click,
            inputs=[traffic_state, traffic_roi_label],
            outputs=[traffic_canvas, traffic_state, traffic_setup_status]
        )
        traffic_roi_label.change(
            fn=on_traffic_roi_label_change,
            inputs=[traffic_roi_label, traffic_state],
            outputs=[traffic_canvas]
        )
        btn_traffic_clear_roi.click(
            fn=on_traffic_clear_roi,
            inputs=[traffic_state],
            outputs=[traffic_canvas, traffic_state, traffic_setup_status]
        )
        traffic_veh_conf.release(
            fn=on_traffic_veh_conf_changed,
            inputs=[traffic_veh_conf, traffic_state],
            outputs=[traffic_video_display, traffic_stat_table, traffic_alarm_html, traffic_setup_status]
        )



# ==============================================================================
# FASTAPI REALTIME STREAMING ENGINE (PRODUCER - CONSUMER ARCHITECTURE)
# ==============================================================================
import threading

@demo.app.middleware("http")
async def allow_iframe_embedding_middleware(request: Request, call_next):
    response = await call_next(request)
    # Xoá bỏ mọi rào cản iframe từ trình duyệt
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = "frame-ancestors *"
    return response

STREAMING_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-Accel-Buffering": "no",       # TẮT buffering trên Cloudflare / Nginx / Render proxy
    "Connection": "keep-alive"
}

class RealtimeStreamProducer:
    """
    Background Worker (Producer - Consumer Pattern):
    Tách việc đọc video/camera và suy luận AI ra 1 background thread độc lập.
    Client stream chỉ việc lấy LATEST_PROCESSED_FRAME để yield, không bị nghẽn (blocking).
    Tối ưu hóa:
      - imgsz=640 (hoặc 480), half=True (FP16 GPU Tesla T4)
      - return_bgr=True (không chuyển đổi RGB <-> BGR)
      - Mask caching cho Flood: chạy YOLO-seg mỗi 5 frames, các frame giữa tái sử dụng mask
      - Nén JPEG chất lượng 55 (giảm 70% dung lượng băng thông mạng)
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.latest_frames = {"flood": None, "traffic": None, "fusion": None}
        self.latest_events = {
            "flood": threading.Event(),
            "traffic": threading.Event(),
            "fusion": threading.Event()
        }
        self.workers = {}
        self.running = {"flood": False, "traffic": False, "fusion": False}
        self.active_subscribers = {"flood": 0, "traffic": 0, "fusion": 0}

    def start_worker(self, mode: str, video_source: str, flood_conf: float = 0.25, veh_conf: float = 0.25):
        with self.lock:
            self.active_subscribers[mode] = self.active_subscribers.get(mode, 0) + 1
            if self.running.get(mode, False):
                return
            self.running[mode] = True
            t = threading.Thread(
                target=self._worker_loop,
                args=(mode, video_source, flood_conf, veh_conf),
                daemon=True,
                name=f"Worker-{mode}"
            )
            self.workers[mode] = t
            t.start()

    def stop_worker(self, mode: str):
        with self.lock:
            subs = self.active_subscribers.get(mode, 1) - 1
            self.active_subscribers[mode] = max(0, subs)
            if self.active_subscribers[mode] == 0:
                self.running[mode] = False

    def _worker_loop(self, mode: str, video_source: str, flood_conf: float, veh_conf: float):
        clean_path = get_clean_filepath(video_source)
        cap = cv2.VideoCapture(clean_path)
        fps_vid = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frame_interval = 1.0 / max(10.0, min(30.0, fps_vid))

        active_flood = get_selected_flood_model()
        frame_idx = 0
        last_flood_hud_bgr = None

        while self.running.get(mode, False) and LIVE_STREAM_STATE.get("active", True):
            t0 = time.time()
            ret, frame = cap.read()
            if not ret or frame is None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                frame_idx = 0
                continue

            frame_idx += 1
            h, w = frame.shape[:2]
            if w > 640:
                frame = cv2.resize(frame, (640, int(h * 640.0 / w)), interpolation=cv2.INTER_LINEAR)

            out_bgr = frame
            with torch.inference_mode():
                if mode == "flood":
                    out_bgr, _, _, _ = process_single_frame_hybrid(
                        frame, global_multi_gauge_manager, global_tab5_arbiter,
                        active_flood, None, flood_conf=float(flood_conf), mode="Cột mốc",
                        is_streaming=True, return_bgr=True
                    )
                elif mode == "traffic":
                    sim_t = frame_idx / fps_vid
                    out_bgr, _, _, _ = global_traffic_analyzer.analyze_frame(
                        frame, vehicle_model, veh_conf=float(veh_conf), fps=fps_vid,
                        is_single_image=False, location_name="Màn hình camera", sim_time_s=sim_t,
                        return_bgr=True, is_streaming=True
                    )
                elif mode == "fusion":
                    # Tối ưu Mask Caching: Chạy phân đoạn ngập mỗi 5 frames
                    if frame_idx % 5 == 1 or last_flood_hud_bgr is None:
                        flood_hud_bgr, _, _, _ = process_single_frame_hybrid(
                            frame, global_multi_gauge_manager, global_tab5_arbiter,
                            active_flood, None, flood_conf=float(flood_conf), mode="Cột mốc",
                            is_streaming=True, return_bgr=True
                        )
                        last_flood_hud_bgr = flood_hud_bgr
                    else:
                        flood_hud_bgr = frame

                    sim_t = frame_idx / fps_vid
                    out_bgr, _, _, _ = global_traffic_analyzer.analyze_frame(
                        last_flood_hud_bgr, vehicle_model, veh_conf=float(veh_conf), fps=fps_vid,
                        is_single_image=False, location_name="Camera Tích Hợp", sim_time_s=sim_t,
                        return_bgr=True, is_streaming=True
                    )

            # Nén JPEG chất lượng 55 (nhẹ, truyền nhanh qua tunnel)
            _, buf = cv2.imencode(".jpg", out_bgr, [cv2.IMWRITE_JPEG_QUALITY, 55])
            frame_bytes = buf.tobytes()

            with self.lock:
                self.latest_frames[mode] = frame_bytes
            self.latest_events[mode].set()

            elapsed = time.time() - t0
            sleep_t = max(0.001, frame_interval - elapsed)
            time.sleep(sleep_t)

        cap.release()
        with self.lock:
            self.running[mode] = False

STREAM_PRODUCER = RealtimeStreamProducer()


def generate_stream_mjpeg(mode: str, flood_conf: float = 0.25, veh_conf: float = 0.25, custom_source: str = None):
    LIVE_STREAM_STATE["active"] = True
    video_source = custom_source or LIVE_STREAM_STATE.get("video_path") or DEFAULT_GAUGE_VIDEO
    STREAM_PRODUCER.start_worker(mode, video_source, flood_conf=flood_conf, veh_conf=veh_conf)

    try:
        while LIVE_STREAM_STATE.get("active", True):
            event = STREAM_PRODUCER.latest_events[mode]
            if event.wait(timeout=1.0):
                event.clear()
                with STREAM_PRODUCER.lock:
                    frame_bytes = STREAM_PRODUCER.latest_frames[mode]
                if frame_bytes:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame_bytes)).encode() + b"\r\n\r\n"
                        + frame_bytes +
                        b"\r\n"
                    )
    finally:
        STREAM_PRODUCER.stop_worker(mode)


@demo.app.get("/api/v1/stream/flood")
def stream_flood_mjpeg(request: Request, flood_conf: float = 0.25):
    return StreamingResponse(
        generate_stream_mjpeg("flood", flood_conf=flood_conf),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers=STREAMING_HEADERS
    )


@demo.app.get("/api/v1/stream/traffic")
def stream_traffic_mjpeg(request: Request, veh_conf: float = 0.25):
    return StreamingResponse(
        generate_stream_mjpeg("traffic", veh_conf=veh_conf),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers=STREAMING_HEADERS
    )


@demo.app.get("/api/v1/stream/fusion")
def stream_fusion_mjpeg(request: Request, flood_conf: float = 0.25, veh_conf: float = 0.25):
    return StreamingResponse(
        generate_stream_mjpeg("fusion", flood_conf=flood_conf, veh_conf=veh_conf),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers=STREAMING_HEADERS
    )


@demo.app.get("/api/v1/stream/live")
def stream_live_mjpeg(request: Request, source: str = None):
    return StreamingResponse(
        generate_stream_mjpeg("fusion", custom_source=source),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers=STREAMING_HEADERS
    )


if __name__ == "__main__":
    demo.queue(default_concurrency_limit=10)
    demo.launch(server_name="0.0.0.0", server_port=7860, share=True, show_error=True)
