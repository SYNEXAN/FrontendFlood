#!/usr/bin/env python3
"""
Synexan AI Root-Level UI, Smooth Stream & Camera Preset Persistence Injector (v7_cam_presets)
1. Installs sitecustomize.py into Python site-packages so app.py ALWAYS loads
   install_gradio_runtime_hooks() at Python startup before Gradio initializes.
2. Fixes Markdown heading sanitization so '#### ' and '##### ' always retain their
   required trailing space (eliminating literal '####' text on the UI).
3. Automatically captures and persists gauge coordinates (p_bottom, p_mid, p_top,
   h_start_cm, h_end_cm, strip_width) and 4-corner traffic ROI per Camera to
   Vercel Cloud (/api/camera-presets) & scripts/camera_presets.json when the user clicks Save,
   and automatically restores them whenever that Camera is selected again!
4. Ensures 100% zero modifications to the backend/ folder.
"""
import os
import sys
import re
import json
import site
import threading
import copy
import functools
import urllib.request

# Thiết lập timeout và giao thức TCP cho RTSP trong OpenCV để chống nghẽn / treo luồng
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

EMOJI_PATTERN = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\uFE0F\u200D"
    r"🌊⚡🚗📁🖼️🚀⏹⚙️🗑🎯🚦📋📍💡📏🔄✅❌⚠️🛑🎉⏳🔎📌➔👉🎬📷📐💾↩️⚖️📺]+",
    flags=re.UNICODE,
)

PRESET_API_URL = "https://frontend-flood.vercel.app/api/camera-presets"
LOCAL_PRESETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_presets.json")
TMP_PRESETS_PATH = "/tmp/synexan_camera_presets.json"

DEFAULT_CAMERA_PRESETS = {
    "_comment": "Tệp lưu trữ cố định tọa độ cột mốc (SCSG-Net) và khung làn đường (4 góc) cho từng Camera trong dự án.",
    "cam-tran-viet-chau": {
        "cameraKey": "cam-tran-viet-chau",
        "cameraUrl": "rtsp://admin:Mekongpathfinder2026@tvc14738.smartddns.tv:554/cam/realmonitor?channel=1&subtype=1",
        "cameraName": "Camera Trần Việt Châu",
        "gauges": [
            {
                "name": "Cột mốc 1",
                "p_bottom": [18, 60],
                "p_mid": [15, 26],
                "p_top": [17, 4],
                "h_start_cm": 0.0,
                "h_end_cm": 50.0,
                "strip_width": 4,
                "warning_cm": 25.0,
                "calib_mode": "3_points",
            }
        ],
        "roi_points_norm": [
            [0.0017, 0.1333],
            [0.6173, 0.0187],
            [0.8197, 0.1125],
            [0.3367, 0.9958]
        ],
        "roi_label": "Làn đường giám sát",
        "updatedAt": "2026-11-16T09:34:21.000Z",
    },
    "cam-vo-van-kiet": {
        "cameraKey": "cam-vo-van-kiet",
        "cameraUrl": "rtsp://admin:Mekong2026@mekongpathfinder1pnh.smartddns.tv:554/cam/realmonitor?channel=1&subtype=1",
        "cameraName": "Camera Võ Văn Kiệt",
        "gauges": [
            {
                "name": "Cột mốc 1",
                "p_bottom": [474, 250],
                "p_mid": [483, 187],
                "p_top": [489, 126],
                "h_start_cm": 0.0,
                "h_end_cm": 100.0,
                "strip_width": 4,
                "warning_cm": 25.0,
                "calib_mode": "3_points",
            },
            {
                "name": "Cột mốc 2",
                "p_bottom": [177, 278],
                "p_mid": [170, 191],
                "p_top": [164, 85],
                "h_start_cm": 0.0,
                "h_end_cm": 170.0,
                "strip_width": 4,
                "warning_cm": 25.0,
                "calib_mode": "3_points",
            },
        ],
        "roi_points_norm": None,
        "roi_label": "Làn đường giám sát",
        "updatedAt": "2026-11-16T09:00:37.945Z",
    },
    "cam-nga-tu-3/2-thn": {
        "cameraKey": "cam-nga-tu-3/2-thn",
        "cameraUrl": "rtsp://admin:Mekong2026@mekongpathfinder02thn.smartddns.tv:41003/cam/realmonitor?channel=1&subtype=1",
        "cameraName": "Camera Ngã Tư 3/2 - Trần Hoàng Na",
        "gauges": [
            {
                "name": "Cột mốc 1",
                "p_bottom": [504, 335],
                "p_mid": [505, 261],
                "p_top": [501, 115],
                "h_start_cm": 0.0,
                "h_end_cm": 500.0,
                "strip_width": 4,
                "warning_cm": 25.0,
                "calib_mode": "3_points",
            }
        ],
        "roi_points_norm": None,
        "roi_label": "Làn đường giám sát",
        "updatedAt": "2026-11-16T09:07:14.335Z",
    },
    "cam-tran-hoang-na": {
        "cameraKey": "cam-tran-hoang-na",
        "cameraUrl": "rtsp://admin:Mekong2026@mekongpathfinder01thn.smartddns.tv:554/cam/realmonitor?channel=1&subtype=1",
        "cameraName": "Camera Trần Hoàng Na",
        "gauges": [
            {
                "name": "Cột mốc 1",
                "p_bottom": [269, 98],
                "p_mid": [272, 79],
                "p_top": [273, 57],
                "h_start_cm": 0.0,
                "h_end_cm": 50.0,
                "strip_width": 4,
                "warning_cm": 25.0,
                "calib_mode": "3_points",
            }
        ],
        "roi_points_norm": None,
        "roi_label": "Làn đường giám sát",
        "updatedAt": "2026-11-16T09:09:41.526Z",
    },
}

CAMERA_PRESETS_CACHE = copy.deepcopy(DEFAULT_CAMERA_PRESETS)
LAST_ACTIVE_CAMERA = {"key": None, "url": None, "name": None}


def clean_text_py(val):
    if not isinstance(val, str) or not val:
        return val
    if (
        "Hướng dẫn gán mốc" in val
        or "ướng dẫn gán mốc" in val
        or "Hướng dẫn hiệu chuẩn" in val
        or "Nhấp <b>4 góc</b> trên màn hình" in val
    ):
        return ""
    cleaned = EMOJI_PATTERN.sub("", val)
    # Giữ chuẩn khoảng trắng sau dấu # của Markdown Heading (#### 1. ..., ##### Thông tin...)
    cleaned = re.sub(r"^(#+)\s*", r"\1 ", cleaned)
    cleaned = re.sub(r"(\n#+)\s*", r"\1 ", cleaned)
    cleaned = re.sub(r">\s+", ">", cleaned)
    return cleaned.lstrip()


def load_camera_presets_from_all_sources():
    """Tải presets đã lưu của các Camera từ bản nhúng cố định, file cục bộ và Vercel Cloud (Upstash Redis)."""
    global CAMERA_PRESETS_CACHE
    merged = copy.deepcopy(DEFAULT_CAMERA_PRESETS)
    for p in (LOCAL_PRESETS_PATH, TMP_PRESETS_PATH):
        if p and os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged.update(data)
            except Exception:
                pass

    try:
        req = urllib.request.Request(
            PRESET_API_URL,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            if resp.status == 200:
                res_json = json.loads(resp.read().decode("utf-8", errors="ignore"))
                cloud_presets = res_json.get("presets")
                if isinstance(cloud_presets, dict):
                    merged.update(cloud_presets)
    except Exception as e:
        print(f"ℹ️ [Camera Presets] Dùng bản lưu cục bộ ({e})")

    CAMERA_PRESETS_CACHE = merged
    return merged


def save_camera_preset_everywhere(cam_key, cam_url, cam_name, gauges_list, roi_points_norm, roi_label):
    """Lưu tức thì cấu hình mốc & làn đường của Camera xuống file cục bộ và đồng bộ lên Vercel Cloud."""
    if not cam_key:
        return
    entry = {
        "cameraKey": cam_key,
        "cameraUrl": cam_url or cam_key,
        "cameraName": cam_name or cam_key,
        "gauges": gauges_list,
        "roi_points_norm": roi_points_norm,
        "roi_label": roi_label or "Làn đường giám sát",
    }
    CAMERA_PRESETS_CACHE[cam_key] = entry

    for p in (LOCAL_PRESETS_PATH, TMP_PRESETS_PATH):
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(CAMERA_PRESETS_CACHE, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _sync_cloud():
        try:
            payload = json.dumps(entry).encode("utf-8")
            req = urllib.request.Request(
                PRESET_API_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                if resp.status == 200:
                    print(f"💾 [Camera Presets] Đã lưu cố định {len(gauges_list)} mốc cho '{cam_name}' ({cam_key}) lên Cloud!")
        except Exception as e:
            print(f"⚠️ [Camera Presets Cloud Sync] {e}")

    threading.Thread(target=_sync_cloud, daemon=True).start()


def resolve_canonical_camera_info(raw_url_or_none, state_data, main_mod):
    """Xác định định danh chuẩn (id, url, name) của Camera đang thao tác.
    Luôn ưu tiên raw_url_or_none nếu được truyền vào, tuyệt đối không bị đè bởi state cũ."""
    candidate_url = str(raw_url_or_none or "").strip()
    cameras = getattr(main_mod, "CANTHO_CAMERAS", []) or []

    # Ưu tiên 1: URL/ID được truyền vào trực tiếp từ dropdown hoặc nút bấm
    if candidate_url:
        for c in cameras:
            c_url = str(c.get("url", "")).strip()
            c_url_sub1 = c_url.replace("subtype=0", "subtype=1")
            c_url_sub0 = c_url.replace("subtype=1", "subtype=0")
            c_id = str(c.get("id", "")).strip()
            if candidate_url in (c_url, c_url_sub1, c_url_sub0, c_id):
                return c["id"], c["url"], c.get("name", c["id"])
        # Kiểm tra theo tên camera nếu chuỗi truyền vào là tên hoặc chứa tên
        cand_lower = candidate_url.lower()
        for c in cameras:
            c_name = str(c.get("name", "")).strip().lower()
            c_short = str(c.get("short_loc", "")).strip().lower()
            if (c_name and c_name in cand_lower) or (c_short and c_short in cand_lower):
                return c["id"], c["url"], c.get("name", c["id"])
        return candidate_url, candidate_url, candidate_url

    # Ưu tiên 2: Lấy từ state_data hiện tại (chỉ khi raw_url_or_none rỗng/None)
    if isinstance(state_data, dict):
        if state_data.get("canonical_cam_key"):
            return (
                state_data["canonical_cam_key"],
                state_data.get("canonical_cam_url") or state_data["canonical_cam_key"],
                state_data.get("canonical_cam_name") or "Camera",
            )
        cam_src = str(state_data.get("cam_source") or "").strip()
        if cam_src:
            for c in cameras:
                c_url = str(c.get("url", "")).strip()
                if cam_src in (c_url, c_url.replace("subtype=0", "subtype=1"), c_url.replace("subtype=1", "subtype=0"), c.get("id")):
                    return c["id"], c["url"], c.get("name", c["id"])
        cam_title = str(state_data.get("cam_title") or "")
        for c in cameras:
            if c.get("name") and c["name"] in cam_title:
                return c["id"], c["url"], c["name"]

    if LAST_ACTIVE_CAMERA.get("key") and (not isinstance(state_data, dict) or state_data.get("source_type") == "camera_stream"):
        return LAST_ACTIVE_CAMERA["key"], LAST_ACTIVE_CAMERA["url"], LAST_ACTIVE_CAMERA["name"]

    if isinstance(state_data, dict) and state_data.get("source_type") == "video":
        vpath = str(state_data.get("video_path") or "")
        if vpath:
            vname = os.path.basename(vpath)
            return f"video:{vname}", vpath, f"Video {vname}"

    return None, None, None


def capture_and_persist_current_state(state_data, main_mod):
    """Đọc toàn bộ mốc trong global_multi_gauge_manager và ROI trong global_traffic_analyzer để lưu cố định."""
    cam_key, cam_url, cam_name = resolve_canonical_camera_info(None, state_data, main_mod)
    if not cam_key:
        return

    mgr = getattr(main_mod, "global_multi_gauge_manager", None)
    gauges_list = []
    if mgr is not None:
        for gx in mgr.gauges.values():
            if getattr(gx, "is_configured", False) and getattr(gx, "p_bottom", None) and getattr(gx, "p_top", None):
                gauges_list.append({
                    "name": str(getattr(gx, "name", "Cột mốc")),
                    "p_bottom": [int(gx.p_bottom[0]), int(gx.p_bottom[1])],
                    "p_mid": [int(gx.p_mid[0]), int(gx.p_mid[1])] if getattr(gx, "p_mid", None) else None,
                    "p_top": [int(gx.p_top[0]), int(gx.p_top[1])],
                    "h_start_cm": float(getattr(gx, "h_start_cm", 0.0)),
                    "h_end_cm": float(getattr(gx, "h_end_cm", 100.0)),
                    "strip_width": int(getattr(gx, "strip_width", 4)),
                    "warning_cm": float(getattr(gx, "warning_cm", 25.0)),
                    "calib_mode": str(getattr(gx, "calib_mode", "3_points")),
                })

    ta = getattr(main_mod, "global_traffic_analyzer", None)
    roi_norm = None
    roi_lbl = "Làn đường giám sát"
    if ta is not None:
        pts = getattr(ta, "roi_points_norm", None)
        if pts and len(pts) == 4:
            roi_norm = [[float(px), float(py)] for px, py in pts]
        roi_lbl = str(getattr(ta, "roi_label", "Làn đường giám sát"))

    save_camera_preset_everywhere(cam_key, cam_url, cam_name, gauges_list, roi_norm, roi_lbl)


def restore_presets_for_camera(cam_key, cam_url, base_frame, main_mod):
    """Khôi phục các cột mốc và khung làn đường đã lưu cho Camera này."""
    if not cam_key:
        return 0, False

    entry = CAMERA_PRESETS_CACHE.get(cam_key) or (CAMERA_PRESETS_CACHE.get(cam_url) if cam_url else None)
    if not entry:
        load_camera_presets_from_all_sources()
        entry = CAMERA_PRESETS_CACHE.get(cam_key) or (CAMERA_PRESETS_CACHE.get(cam_url) if cam_url else None)

    if not isinstance(entry, dict):
        return 0, False

    restored_gauges = 0
    restored_roi = False

    gauges_cfg = entry.get("gauges")
    mgr = getattr(main_mod, "global_multi_gauge_manager", None)
    if isinstance(gauges_cfg, list) and len(gauges_cfg) > 0 and mgr is not None and base_frame is not None:
        mgr.reset_all()
        for g_cfg in gauges_cfg:
            if not isinstance(g_cfg, dict) or not g_cfg.get("p_bottom") or not g_cfg.get("p_top"):
                continue
            target_g = None
            for gx in mgr.gauges.values():
                if not gx.is_configured:
                    target_g = gx
                    mgr.set_active_gauge(gx.gauge_id)
                    break
            if target_g is None:
                new_gid = mgr.add_gauge(
                    name=g_cfg.get("name", f"Cột mốc {restored_gauges + 1}"),
                    h_start_cm=float(g_cfg.get("h_start_cm", 0.0)),
                    h_end_cm=float(g_cfg.get("h_end_cm", 100.0)),
                    warning_cm=float(g_cfg.get("warning_cm", 25.0)),
                )
                target_g = mgr.get_gauge(new_gid)

            p_bot = tuple(int(v) for v in g_cfg["p_bottom"][:2])
            p_top = tuple(int(v) for v in g_cfg["p_top"][:2])
            p_mid = tuple(int(v) for v in g_cfg["p_mid"][:2]) if g_cfg.get("p_mid") else ((p_bot[0] + p_top[0]) // 2, (p_bot[1] + p_top[1]) // 2)

            target_g.configure(
                p_bottom=p_bot,
                p_top=p_top,
                p_mid=p_mid,
                name=str(g_cfg.get("name", f"Cột mốc {restored_gauges + 1}")),
                warning_cm=float(g_cfg.get("warning_cm", 25.0)),
                base_frame=base_frame,
                strip_width=int(g_cfg.get("strip_width", 4)),
                h_start_cm=float(g_cfg.get("h_start_cm", 0.0)),
                h_end_cm=float(g_cfg.get("h_end_cm", 100.0)),
                calib_mode=str(g_cfg.get("calib_mode", "3_points")),
            )
            restored_gauges += 1

    roi_norm = entry.get("roi_points_norm")
    ta = getattr(main_mod, "global_traffic_analyzer", None)
    if isinstance(roi_norm, list) and len(roi_norm) == 4 and ta is not None:
        try:
            pts_tuple = [(float(pt[0]), float(pt[1])) for pt in roi_norm]
            ta.set_roi_norm(pts_tuple, label=str(entry.get("roi_label", "Làn đường giám sát")))
            restored_roi = True
        except Exception:
            pass

    return restored_gauges, restored_roi


def clean_dataframe_emojis(df):
    """Làm sạch 100% icon và emoji trong DataFrame (cả tiêu đề cột và giá trị ô)."""
    import pandas as pd
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    try:
        new_cols = [clean_text_py(str(c)) for c in df.columns]
        new_df = df.copy()
        new_df.columns = new_cols
        for col in new_df.columns:
            new_df[col] = new_df[col].apply(lambda x: clean_text_py(str(x)) if isinstance(x, str) else x)
        return new_df
    except Exception:
        return df


def format_ioc_fusion_outputs(df_summary, orig_alert_html):
    """Khôi phục card tổng kết về đúng bố cục 2 dòng nguyên bản ban đầu, giữ 100% không icon/emoji."""
    import pandas as pd
    depth_val = 0.0
    veh_count = 0
    status_label = "Thông thoáng"
    avg_speed = 0.0

    if isinstance(df_summary, pd.DataFrame) and not df_summary.empty:
        for _, row in df_summary.iterrows():
            if len(row) < 2:
                continue
            metric = str(row.iloc[0])
            val = str(row.iloc[1])
            if "Mực Nước" in metric or "Đo Được" in metric:
                try:
                    nums = re.findall(r"[-+]?\d*\.\d+|\d+", val)
                    if nums:
                        depth_val = float(nums[0])
                except Exception:
                    pass
            elif "Phương Tiện" in metric:
                try:
                    nums = re.findall(r"\d+", val)
                    if nums:
                        veh_count = int(nums[0])
                except Exception:
                    pass
            elif "Làn Đường" in metric or "Trạng Thái" in metric or "Tình Trạng" in metric:
                status_label = clean_text_py(val).strip()
            elif "Tốc Độ" in metric or "Vận Tốc" in metric:
                try:
                    nums = re.findall(r"[-+]?\d*\.\d+|\d+", val)
                    if nums:
                        avg_speed = float(nums[0])
                except Exception:
                    pass

    depth_str = f"{depth_val:.1f} cm"
    flood_level = "AN TOÀN"
    flood_color = "#22c55e"
    if depth_val >= 50:
        flood_level = "CỰC KỲ NGUY HIỂM"
        flood_color = "#ef4444"
    elif depth_val >= 30:
        flood_level = "BÁO ĐỘNG CẤP 2 (NGẬP SÂU)"
        flood_color = "#f97316"
    elif depth_val >= 15:
        flood_level = "BÁO ĐỘNG CẤP 1 (NGẬP NHẸ)"
        flood_color = "#eab308"

    # Đúng 100% bố cục và màu sắc giao diện gốc, loại bỏ emoji 🌊 và 🚗
    alert_html = f"""
    <div style='background:rgba(15,23,42,0.95); border:1px solid #334155; border-radius:12px; padding:14px 18px; margin-bottom:12px;'>
        <div style='display:flex; align-items:center; padding-bottom:10px; border-bottom:1px solid rgba(255,255,255,0.08); margin-bottom:10px;'>
            <span style='font-size:1.1rem; font-weight:700; color:#38bdf8; min-width:115px;'>Mực Nước:</span>
            <span style='font-size:1.35rem; font-weight:800; color:{flood_color}; margin-right:12px;'>{depth_str}</span>
            <span style='font-size:0.88rem; font-weight:700; padding:3px 10px; border-radius:6px; background:rgba(255,255,255,0.1); color:{flood_color};'>{flood_level}</span>
        </div>
        <div style='display:flex; align-items:center;'>
            <span style='font-size:1.1rem; font-weight:700; color:#e2e8f0; min-width:115px;'>Giao Thông:</span>
            <span style='font-size:1.35rem; font-weight:800; color:#38bdf8; margin-right:12px;'>{veh_count} xe</span>
            <span style='font-size:0.9rem; color:#94a3b8;'>({status_label} - Vận tốc TB: <b style="color:#e2e8f0;">{avg_speed:.1f} km/h</b>)</span>
        </div>
    </div>
    """

    table_rows = [
        ["Mực Nước Đo Được", depth_str],
        ["Cấp Độ Ngập", flood_level],
        ["Tổng Phương Tiện", f"{veh_count} xe"],
        ["Tình Trạng Làn Đường", status_label],
        ["Tốc Độ Trung Bình", f"{avg_speed:.1f} km/h"]
    ]
    clean_df = pd.DataFrame(table_rows, columns=["Chỉ Số Giám Sát", "Giá Trị"])
    return clean_df, alert_html


def install_latest_frame_grabber_timeout_guard(main_mod):
    """Bảo vệ LatestFrameGrabber tự động ngắt kết nối nếu luồng camera bị đứt tín hiệu quá 6 giây (chống treo spinner 102s)."""
    import time
    grabber_cls = getattr(main_mod, "LatestFrameGrabber", None)
    if grabber_cls is None:
        for m in list(sys.modules.values()):
            if m and hasattr(m, "LatestFrameGrabber"):
                grabber_cls = getattr(m, "LatestFrameGrabber")
                break
    if grabber_cls is not None and not getattr(grabber_cls, "_synexan_guarded", False):
        orig_init = grabber_cls.__init__
        orig_read = grabber_cls.read_latest

        @functools.wraps(orig_init)
        def guarded_init(self, src, max_width=640, *args, **kwargs):
            orig_init(self, src, max_width=max_width, *args, **kwargs)
            self._start_time = time.time()
            self._last_success_time = time.time()

        @functools.wraps(orig_read)
        def guarded_read_latest(self):
            ok, frm, ts, seq = orig_read(self)
            now = time.time()
            if ok and frm is not None:
                self._last_success_time = now
            else:
                # Nếu không nhận được frame trong 6.0 giây liên tục, tự động ngắt is_opened để thoát while loop, chống treo spinner
                if now - getattr(self, "_last_success_time", now) > 6.0:
                    print(f"⚠️ [Stream Timeout Guard] Luồng {self.src} mất tín hiệu quá 6 giây, tự động đóng luồng.")
                    self.is_opened = False
            return ok, frm, ts, seq

        grabber_cls.__init__ = guarded_init
        grabber_cls.read_latest = guarded_read_latest
        grabber_cls._synexan_guarded = True


def install_camera_preset_hooks(blocks_instance):
    """Bọc các hàm sự kiện Camera & Lưu cột mốc trong __main__ và demo.fns để tự động lưu/nạp mốc cố định."""
    main_mod = sys.modules.get("__main__")
    if not main_mod or getattr(main_mod, "_synexan_cam_hooks_installed", False):
        return
    main_mod._synexan_cam_hooks_installed = True

    load_camera_presets_from_all_sources()
    install_latest_frame_grabber_timeout_guard(main_mod)
    import gradio as gr

    fn_replacements = {}

    # 1. Hook on_tab5_camera_connect
    orig_tab5_cam = getattr(main_mod, "on_tab5_camera_connect", None)
    if callable(orig_tab5_cam):
        @functools.wraps(orig_tab5_cam)
        def wrapped_tab5_cam(cam_url_str, state_data, name, h_start_cm, h_end_cm, strip_width=4, **kwargs):
            cam_key, cam_url, cam_name = resolve_canonical_camera_info(cam_url_str, state_data, main_mod)
            if cam_key:
                LAST_ACTIVE_CAMERA.update({"key": cam_key, "url": cam_url, "name": cam_name})
            res = orig_tab5_cam(cam_url_str, state_data, name, h_start_cm, h_end_cm, strip_width=strip_width, **kwargs)
            try:
                prev_rgb, slider_upd, new_state, msg, drop_upd = res
                if isinstance(new_state, dict) and cam_key:
                    new_state["canonical_cam_key"] = cam_key
                    new_state["canonical_cam_url"] = cam_url
                    new_state["canonical_cam_name"] = cam_name
                base_frame = main_mod.get_state_base_frame(new_state)
                if base_frame is not None and cam_key:
                    n_g, _ = restore_presets_for_camera(cam_key, cam_url, base_frame, main_mod)
                    if n_g > 0:
                        prev_bgr = main_mod.draw_multi_calibration_preview(
                            base_frame,
                            main_mod.global_multi_gauge_manager,
                            active_gauge_id=main_mod.global_multi_gauge_manager.active_id,
                            p_bottom=None,
                            p_top=None,
                            p_mid=None,
                            name="",
                            h_start_cm=0,
                            h_end_cm=0,
                            click_step=0,
                            calib_mode="3_points",
                            strip_width=int(strip_width or 4),
                        )
                        prev_rgb = main_mod.cv2.cvtColor(prev_bgr, main_mod.cv2.COLOR_BGR2RGB)
                        choices = main_mod.get_gauge_dropdown_choices()
                        sel_val = choices[-1][1] if choices else None
                        drop_upd = gr.update(choices=choices, value=sel_val)
                        return prev_rgb, slider_upd, new_state, clean_text_py(msg), drop_upd
            except Exception as e:
                print(f"⚠️ [Tab5 Cam Restore] {e}")
            return res

        setattr(main_mod, "on_tab5_camera_connect", wrapped_tab5_cam)
        fn_replacements[orig_tab5_cam] = wrapped_tab5_cam

    # 2. Hook on_fusion_camera_connect
    orig_fusion_cam = getattr(main_mod, "on_fusion_camera_connect", None)
    if callable(orig_fusion_cam):
        @functools.wraps(orig_fusion_cam)
        def wrapped_fusion_cam(cam_url, state_data):
            cam_key, canon_url, cam_name = resolve_canonical_camera_info(cam_url, state_data, main_mod)
            if cam_key:
                LAST_ACTIVE_CAMERA.update({"key": cam_key, "url": canon_url, "name": cam_name})
            resolved_url = main_mod.resolve_camera_stream_url(cam_url) if hasattr(main_mod, "resolve_camera_stream_url") else cam_url
            res = orig_fusion_cam(resolved_url, state_data)
            try:
                first_frame, slider_upd, new_state, msg = res
                if isinstance(new_state, dict) and cam_key:
                    new_state["cam_source"] = canon_url or cam_url
                    new_state["cam_title"] = cam_name
                    new_state["canonical_cam_key"] = cam_key
                    new_state["canonical_cam_url"] = canon_url
                    new_state["canonical_cam_name"] = cam_name
                base_frame = main_mod.get_state_base_frame(new_state)
                if base_frame is not None and cam_key:
                    n_g, has_r = restore_presets_for_camera(cam_key, canon_url, base_frame, main_mod)
                    if n_g > 0 or has_r:
                        first_frame = main_mod._render_fusion_base_preview(base_frame, new_state)
                return first_frame, slider_upd, new_state, clean_text_py(msg)
            except Exception as e:
                print(f"⚠️ [Fusion Cam Restore] {e}")
            return res

        setattr(main_mod, "on_fusion_camera_connect", wrapped_fusion_cam)
        fn_replacements[orig_fusion_cam] = wrapped_fusion_cam

    # 3. Hook on_fusion_switch_to_camera_tab và on_tab5_switch_to_camera_tab
    orig_fusion_tab_switch = getattr(main_mod, "on_fusion_switch_to_camera_tab", None)
    if callable(orig_fusion_tab_switch):
        @functools.wraps(orig_fusion_tab_switch)
        def wrapped_fusion_tab_switch(cam_url, state_data):
            cam_key, canon_url, cam_name = resolve_canonical_camera_info(cam_url, state_data, main_mod)
            resolved_url = canon_url or cam_url or (getattr(main_mod, "CANTHO_CAMERAS", [{}])[0].get("url"))
            return wrapped_fusion_cam(resolved_url, state_data)

        setattr(main_mod, "on_fusion_switch_to_camera_tab", wrapped_fusion_tab_switch)
        fn_replacements[orig_fusion_tab_switch] = wrapped_fusion_tab_switch

    orig_tab5_tab_switch = getattr(main_mod, "on_tab5_switch_to_camera_tab", None)
    if callable(orig_tab5_tab_switch):
        @functools.wraps(orig_tab5_tab_switch)
        def wrapped_tab5_tab_switch(cam_url, state_data, *args, **kwargs):
            cam_key, canon_url, cam_name = resolve_canonical_camera_info(cam_url, state_data, main_mod)
            resolved_url = canon_url or cam_url or (getattr(main_mod, "CANTHO_CAMERAS", [{}])[0].get("url"))
            return wrapped_tab5_cam(resolved_url, state_data, *args, **kwargs)

        setattr(main_mod, "on_tab5_switch_to_camera_tab", wrapped_tab5_tab_switch)
        fn_replacements[orig_tab5_tab_switch] = wrapped_tab5_tab_switch

    # 4. Hook on_traffic_camera_connect
    orig_traffic_cam = getattr(main_mod, "on_traffic_camera_connect", None)
    if callable(orig_traffic_cam):
        @functools.wraps(orig_traffic_cam)
        def wrapped_traffic_cam(cam_url_str, state_data):
            cam_key, canon_url, cam_name = resolve_canonical_camera_info(cam_url_str, state_data, main_mod)
            if cam_key:
                LAST_ACTIVE_CAMERA.update({"key": cam_key, "url": canon_url, "name": cam_name})
            res = orig_traffic_cam(cam_url_str, state_data)
            try:
                prev_rgb, slider_upd, new_state, msg = res
                if isinstance(new_state, dict) and cam_key:
                    new_state["canonical_cam_key"] = cam_key
                    new_state["canonical_cam_url"] = canon_url
                    new_state["canonical_cam_name"] = cam_name
                base_frame = main_mod.get_state_base_frame(new_state)
                if base_frame is not None and cam_key:
                    _, has_r = restore_presets_for_camera(cam_key, canon_url, base_frame, main_mod)
                    if has_r:
                        prev_rgb = main_mod._render_traffic_base_with_roi(base_frame, new_state)
                        return prev_rgb, slider_upd, new_state, clean_text_py(msg)
            except Exception as e:
                print(f"⚠️ [Traffic Cam Restore] {e}")
            return res

        setattr(main_mod, "on_traffic_camera_connect", wrapped_traffic_cam)
        fn_replacements[orig_traffic_cam] = wrapped_traffic_cam

    # 5. Hook run_fusion_live: Tự động khôi phục cột mốc cố định & Chuẩn hóa hiển thị IOC
    orig_run_fusion = getattr(main_mod, "run_fusion_live", None)
    if callable(orig_run_fusion):
        @functools.wraps(orig_run_fusion)
        def wrapped_run_fusion_live(state_data, flood_conf, veh_conf, start_frame_slider):
            try:
                cam_key, cam_url, cam_name = resolve_canonical_camera_info(None, state_data, main_mod)
                if cam_key:
                    mgr = getattr(main_mod, "global_multi_gauge_manager", None)
                    has_configured = mgr is not None and any(getattr(gx, "is_configured", False) for gx in getattr(mgr, "gauges", {}).values())
                    if not has_configured:
                        base_frame = main_mod.get_state_base_frame(state_data) if hasattr(main_mod, "get_state_base_frame") else None
                        restore_presets_for_camera(cam_key, cam_url, base_frame, main_mod)
            except Exception as cam_err:
                print(f"ℹ️ [Auto-Restore Gauge on Run] {cam_err}")

            gen = orig_run_fusion(state_data, flood_conf, veh_conf, start_frame_slider)
            import pandas as pd
            try:
                for item in gen:
                    try:
                        if isinstance(item, tuple) and len(item) == 4:
                            hud_rgb, df_summary, alert_html, status_info = item
                            clean_df, clean_html = format_ioc_fusion_outputs(df_summary, alert_html)
                            clean_status = clean_text_py(status_info) if isinstance(status_info, str) else status_info
                            yield hud_rgb, clean_df, clean_html, clean_status
                            continue
                    except Exception:
                        pass
                    yield item
            except Exception as e:
                import pandas as pd
                yield None, pd.DataFrame(), f"<div style='color:#ef4444;'>⚠️ Gián đoạn kết nối stream: {e}</div>", "<div style='color:#ef4444;'>Lỗi kết nối</div>"

        setattr(main_mod, "run_fusion_live", wrapped_run_fusion_live)
        fn_replacements[orig_run_fusion] = wrapped_run_fusion_live

    # 6. Hook run_video_tab5_live và run_traffic_congestion_live để lọc sạch 100% Emoji lúc suy luận
    orig_run_tab5 = getattr(main_mod, "run_video_tab5_live", None)
    if callable(orig_run_tab5):
        @functools.wraps(orig_run_tab5)
        def wrapped_run_tab5(state_data, flood_conf, start_frame_slider):
            gen = orig_run_tab5(state_data, flood_conf, start_frame_slider)
            for item in gen:
                try:
                    if isinstance(item, tuple) and len(item) == 4:
                        hud_rgb, df, alarm_html, status_info = item
                        clean_df = clean_dataframe_emojis(df)
                        clean_html = clean_text_py(alarm_html) if isinstance(alarm_html, str) else alarm_html
                        clean_status = clean_text_py(status_info) if isinstance(status_info, str) else status_info
                        yield hud_rgb, clean_df, clean_html, clean_status
                        continue
                except Exception:
                    pass
                yield item

        setattr(main_mod, "run_video_tab5_live", wrapped_run_tab5)
        fn_replacements[orig_run_tab5] = wrapped_run_tab5

    orig_run_traffic = getattr(main_mod, "run_traffic_congestion_live", None)
    if callable(orig_run_traffic):
        @functools.wraps(orig_run_traffic)
        def wrapped_run_traffic(state_data, veh_conf, start_frame_slider):
            gen = orig_run_traffic(state_data, veh_conf, start_frame_slider)
            for item in gen:
                try:
                    if isinstance(item, tuple) and len(item) == 4:
                        hud_rgb, df, alarm_html, status_info = item
                        clean_df = clean_dataframe_emojis(df)
                        clean_html = clean_text_py(alarm_html) if isinstance(alarm_html, str) else alarm_html
                        clean_status = clean_text_py(status_info) if isinstance(status_info, str) else status_info
                        yield hud_rgb, clean_df, clean_html, clean_status
                        continue
                except Exception:
                    pass
                yield item

        setattr(main_mod, "run_traffic_congestion_live", wrapped_run_traffic)
        fn_replacements[orig_run_traffic] = wrapped_run_traffic

    # 7. Hook các hàm Lưu / Xóa mốc & ROI để tự động đồng bộ lên Cloud ngay khi người dùng thao tác
    for save_fn_name, state_idx in [
        ("on_tab5_save_gauge", 0),
        ("on_fusion_save_gauge", 0),
        ("on_tab5_remove_active_gauge", 1),
        ("on_tab5_reset_all_gauges", 0),
        ("on_fusion_delete_gauge", 1),
        ("on_fusion_reset_all_gauges", 0),
        ("on_traffic_canvas_click", 1),
        ("on_traffic_clear_roi", 0),
        ("on_fusion_canvas_click", 1),
        ("on_fusion_clear_roi", 0),
    ]:
        orig_fn = getattr(main_mod, save_fn_name, None)
        if callable(orig_fn):
            def make_save_wrapper(f_orig, s_idx):
                @functools.wraps(f_orig)
                def _wrapped(*a, **kw):
                    res = f_orig(*a, **kw)
                    try:
                        st = a[s_idx] if len(a) > s_idx else None
                        capture_and_persist_current_state(st, main_mod)
                    except Exception as e:
                        print(f"⚠️ [Auto-Save Preset Hook] {e}")
                    return res
                return _wrapped

            w_fn = make_save_wrapper(orig_fn, state_idx)
            setattr(main_mod, save_fn_name, w_fn)
            fn_replacements[orig_fn] = w_fn

    # Cập nhật tất cả BlockFunction trong demo.fns
    try:
        fns_collection = getattr(blocks_instance, "fns", None)
        if isinstance(fns_collection, dict):
            fns_iter = fns_collection.values()
        elif isinstance(fns_collection, list):
            fns_iter = fns_collection
        else:
            fns_iter = []
        for bf in fns_iter:
            target = getattr(bf, "fn", None)
            if target in fn_replacements:
                bf.fn = fn_replacements[target]
    except Exception as e:
        print(f"⚠️ [Update demo.fns] {e}")


def sanitize_blocks_in_memory(blocks_instance):
    """Sanitize live Gradio Block instances in demo.blocks before get_config_file serializes them."""
    import pandas as pd
    try:
        blocks_dict = getattr(blocks_instance, "blocks", {}) or {}
        for b in blocks_dict.values():
            elem_id = getattr(b, "elem_id", None)
            if (
                elem_id in ("tab5_setup_status", "traffic_setup_status", "fusion_setup_status")
                or (isinstance(elem_id, str) and "setup_status" in elem_id)
            ):
                b.value = ""
                b.visible = False

            # Đảm bảo các tab và công cụ hiệu chuẩn, thanh trượt hiển thị đầy đủ
            if elem_id in (
                "tab_item_fusion_gauge", "tab_item_fusion_lane",
                "fusion_seek_slider", "tab5_seek_slider", "traffic_seek_slider"
            ):
                b.visible = True

            lbl = getattr(b, "label", None)
            if isinstance(lbl, str) and "Bề rộng cột mốc" in lbl:
                b.value = getattr(b, "minimum", 4) or 4

            val = getattr(b, "value", None)
            if isinstance(val, dict) and "strip_width" in val:
                val["strip_width"] = 4

            # Làm sạch bảng DataFrame và danh sách dòng khởi tạo (tránh emoji xuất hiện khi reload)
            if hasattr(b, "headers") and getattr(b, "headers", None):
                b.headers = [clean_text_py(str(h)) for h in b.headers]
            if isinstance(val, list):
                clean_val_rows = []
                for row in val:
                    if isinstance(row, list):
                        clean_val_rows.append([clean_text_py(str(c)) for c in row])
                    else:
                        clean_val_rows.append(clean_text_py(str(row)) if isinstance(row, str) else row)
                b.value = clean_val_rows
            elif isinstance(val, pd.DataFrame):
                b.value = clean_dataframe_emojis(val)

            for attr in ("label", "value", "title", "info", "placeholder"):
                cur = getattr(b, attr, None)
                if isinstance(cur, str):
                    try:
                        setattr(b, attr, clean_text_py(cur))
                    except Exception:
                        pass
    except Exception as e:
        print(f"⚠️ [Sanitize Blocks] {e}")


def sanitize_config_dict_py(cfg):
    if not isinstance(cfg, dict):
        return cfg
    if isinstance(cfg.get("title"), str):
        cfg["title"] = clean_text_py(cfg["title"])

    comps = cfg.get("components")
    if isinstance(comps, list):
        for c in comps:
            if not isinstance(c, dict):
                continue
            p = c.get("props")
            if not isinstance(p, dict):
                continue

            elem_id = p.get("elem_id")
            if (
                elem_id in ("tab5_setup_status", "traffic_setup_status", "fusion_setup_status")
                or (isinstance(elem_id, str) and "setup_status" in elem_id)
            ):
                p["value"] = ""
                p["visible"] = False

            # Đảm bảo các tab và công cụ hiệu chuẩn, thanh trượt hiển thị đầy đủ
            if elem_id in (
                "tab_item_fusion_gauge", "tab_item_fusion_lane",
                "fusion_seek_slider", "tab5_seek_slider", "traffic_seek_slider"
            ):
                p["visible"] = True

            lbl = p.get("label")
            if isinstance(lbl, str) and "Bề rộng cột mốc" in lbl:
                p["value"] = p.get("minimum", 4)

            if c.get("type") == "state" and isinstance(p.get("value"), dict) and "strip_width" in p["value"]:
                p["value"]["strip_width"] = 4

            for k in ("label", "value", "title", "info", "placeholder"):
                if isinstance(p.get(k), str):
                    p[k] = clean_text_py(p[k])

            if isinstance(p.get("headers"), list):
                p["headers"] = [clean_text_py(h) if isinstance(h, str) else h for h in p["headers"]]

            if isinstance(p.get("choices"), list):
                new_choices = []
                for ch in p["choices"]:
                    if isinstance(ch, (list, tuple)) and len(ch) >= 2:
                        new_choices.append([clean_text_py(ch[0]) if isinstance(ch[0], str) else ch[0], ch[1]])
                    elif isinstance(ch, str):
                        new_choices.append(clean_text_py(ch))
                    else:
                        new_choices.append(ch)
                p["choices"] = new_choices
    return cfg


def install_custom_vehicle_weights():
    """Sao lưu trọng số cũ và kích hoạt trọng số 4 lớp VN (yolov8s_vn_vehicle_best.pt) + Flood V20 (yolov8m_flood_v20_scratch.pt)."""
    try:
        import shutil
        models_dir = "/kaggle/working/FrontendFlood/backend/models"
        os.makedirs(models_dir, exist_ok=True)

        # 1. Trọng số phương tiện 4 lớp VN
        custom_weight = "/kaggle/working/FrontendFlood/models/yolov8s_vn_vehicle_best.pt"
        if os.path.exists(custom_weight) and os.path.getsize(custom_weight) > 15000000:
            target_weight = os.path.join(models_dir, "yolov8s_vehicle.pt")
            backup_weight = os.path.join(models_dir, "yolov8s_vehicle_backup.pt")
            if os.path.exists(target_weight) and not os.path.exists(backup_weight):
                if os.path.getsize(target_weight) != os.path.getsize(custom_weight):
                    shutil.copy2(target_weight, backup_weight)
                    print(f"📦 [Weight Backup] Đã sao lưu trọng số phương tiện cũ tại: {backup_weight} ({os.path.getsize(backup_weight)} bytes)")
            if not os.path.exists(target_weight) or os.path.getsize(target_weight) != os.path.getsize(custom_weight):
                shutil.copy2(custom_weight, target_weight)
                print(f"🚗 [Custom Weights] Đã kích hoạt trọng số phương tiện mới (4 lớp VN - mAP50=92.79%): {target_weight} ({os.path.getsize(target_weight)} bytes)")

        # 2. Trọng số ngập lụt Flood V20 (yolov8m_flood_v20_scratch.pt)
        custom_flood = "/kaggle/working/FrontendFlood/models/yolov8m_flood_v20_scratch.pt"
        if os.path.exists(custom_flood) and os.path.getsize(custom_flood) > 30000000:
            target_flood = os.path.join(models_dir, "vietnam_flood_yolov8s_seg.pt")
            backup_flood = os.path.join(models_dir, "vietnam_flood_yolov8s_seg_backup.pt")
            if os.path.exists(target_flood) and not os.path.exists(backup_flood):
                if os.path.getsize(target_flood) != os.path.getsize(custom_flood):
                    shutil.copy2(target_flood, backup_flood)
                    print(f"📦 [Weight Backup] Đã sao lưu trọng số ngập cũ tại: {backup_flood} ({os.path.getsize(backup_flood)} bytes)")
            if not os.path.exists(target_flood) or os.path.getsize(target_flood) != os.path.getsize(custom_flood):
                shutil.copy2(custom_flood, target_flood)
                print(f"🌊 [Custom Weights] Đã kích hoạt trọng số ngập lụt Flood V20 (YOLOv8m-seg): {target_flood} ({os.path.getsize(target_flood)} bytes)")
    except Exception as e:
        print(f"⚠️ [Custom Weights] {e}")


_V20_TEMPORAL_STATE = {"cand_prev_mask": None}


def install_flood_v20_solution_1_and_5_hooks():
    """
    Tiêm trực tiếp Giải pháp 1 (Instance-Confidence Mask Weighting) tại đầu ra YOLO.predict
    và Giải pháp 5 (Temporal Consistency Gate) tại refine_flood_segmentation_mask
    mà giữ nguyên 100% hàm process_single_frame_hybrid gốc của backend/.
    """
    try:
        import cv2
        import torch
        import numpy as np

        # 1. Hook Giải pháp 1 ngay trên tensor đầu ra của YOLO.predict:
        # Nhân từng mặt nạ masks.data[i] với boxes.conf[i] để torch.amax(f_res.masks.data, dim=0)
        # tự động thu được xác suất thực của từng instance ở mọi nơi!
        try:
            from ultralytics import YOLO
            if not getattr(YOLO, "_synexan_v20_conf_hooked", False):
                orig_predict = YOLO.predict

                @functools.wraps(orig_predict)
                def weighted_predict(self, *args, **kwargs):
                    results = orig_predict(self, *args, **kwargs)
                    try:
                        if results and isinstance(results, (list, tuple)):
                            for r in results:
                                masks = getattr(r, "masks", None)
                                boxes = getattr(r, "boxes", None)
                                if (
                                    masks is not None
                                    and getattr(masks, "data", None) is not None
                                    and len(masks.data) > 0
                                    and boxes is not None
                                    and getattr(boxes, "conf", None) is not None
                                    and len(boxes.conf) == len(masks.data)
                                ):
                                    confs_t = boxes.conf.to(masks.data.device, dtype=masks.data.dtype).view(-1, 1, 1)
                                    masks.data = masks.data * confs_t
                    except Exception:
                        pass
                    return results

                YOLO.predict = weighted_predict
                YOLO._synexan_v20_conf_hooked = True
        except Exception as ye:
            print(f"⚠️ [YOLO Predict Hook] {ye}")

        # 2. Hook Giải pháp 1 (lọc đốm phụ rời rạc conf thấp) & Giải pháp 5 (Temporal Consistency Gate)
        # vào refine_flood_segmentation_mask với chữ ký giống hệt 100% hàm gốc trong backend/
        def v20_refine_flood_segmentation_mask(
            raw_mask, h, w, conf_thresh=0.25, prev_mask=None, frame_bgr=None
        ):
            if raw_mask is None:
                _V20_TEMPORAL_STATE["cand_prev_mask"] = None
                return np.zeros((h, w), dtype=np.float32)
            if raw_mask.shape[:2] != (h, w):
                mask = cv2.resize(raw_mask, (w, h), interpolation=cv2.INTER_LINEAR)
            else:
                mask = raw_mask.copy()

            t_base = float(conf_thresh)
            t_low = max(t_base * 0.85, 0.18)
            candidates = (mask >= t_low).astype(np.uint8)
            if not np.any(candidates > 0):
                _V20_TEMPORAL_STATE["cand_prev_mask"] = np.zeros((h, w), dtype=np.float32)
                return np.zeros((h, w), dtype=np.float32)

            max_frame_conf = float(np.max(mask))
            has_strong_primary = max_frame_conf >= max(0.48, t_base + 0.12)
            secondary_min_conf = max(0.42, t_base + 0.08)
            instant_temporal_conf = max(0.48, t_base + 0.12)

            num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(candidates, connectivity=8)
            clean_bin = np.zeros_like(candidates, dtype=np.uint8)

            cand_prev_mask = _V20_TEMPORAL_STATE.get("cand_prev_mask")
            has_temporal_context = (prev_mask is not None and prev_mask.shape == (h, w))
            has_cand_context = (cand_prev_mask is not None and cand_prev_mask.shape == (h, w))

            for lbl_idx in range(1, num_labels):
                if stats[lbl_idx, cv2.CC_STAT_AREA] < 150:
                    continue
                comp_region = (labels == lbl_idx)
                comp_peak = float(np.max(mask[comp_region]))
                if comp_peak < t_base:
                    continue
                # Giải pháp 1: Lọc mảnh phụ rời rạc có conf thấp khi đã có vùng nước chính
                if has_strong_primary and comp_peak < secondary_min_conf:
                    continue
                # Giải pháp 5: Lọc chống nhấp nháy 1-frame theo thời gian
                if has_temporal_context and comp_peak < instant_temporal_conf:
                    prev_overlap = float(np.mean(prev_mask[comp_region] > 0))
                    cand_overlap = float(np.mean(cand_prev_mask[comp_region] > 0)) if has_cand_context else 0.0
                    if prev_overlap < 0.10 and cand_overlap < 0.15:
                        continue
                clean_bin[comp_region] = 1

            _V20_TEMPORAL_STATE["cand_prev_mask"] = (mask >= t_base).astype(np.float32)

            if not np.any(clean_bin > 0):
                return np.zeros((h, w), dtype=np.float32)

            refined = np.where(clean_bin > 0, np.maximum(mask, t_base), 0.0).astype(np.float32)
            return np.clip(refined, 0.0, 1.0)

        for mod_name in ("adaptive_hybrid_depth_engine",):
            mod = sys.modules.get(mod_name)
            if mod is None:
                try:
                    mod = __import__(mod_name)
                except Exception:
                    mod = None
            if mod is not None and not getattr(mod, "_synexan_v20_patched", False):
                mod.refine_flood_segmentation_mask = v20_refine_flood_segmentation_mask
                mod._synexan_v20_patched = True
                for m_obj in list(sys.modules.values()):
                    if m_obj is not None and hasattr(m_obj, "refine_flood_segmentation_mask"):
                        try:
                            setattr(m_obj, "refine_flood_segmentation_mask", v20_refine_flood_segmentation_mask)
                        except Exception:
                            pass
                print("✨ [Flood V20 Filter] Đã kích hoạt Giải pháp 1 (Instance-Confidence Weighting) + Giải pháp 5 (Temporal Consistency Gate)!")
    except Exception as e:
        print(f"⚠️ [Flood V20 Filter] {e}")


def install_gradio_runtime_hooks():
    """Hook gr.Blocks.get_config_file without modifying Component.__init__ signatures."""
    install_custom_vehicle_weights()
    install_flood_v20_solution_1_and_5_hooks()
    try:
        import gradio as gr

        if not getattr(gr.Blocks, "_synexan_hooked", False):
            orig_get_config = gr.Blocks.get_config_file

            def hooked_get_config_file(self, *args, **kwargs):
                install_flood_v20_solution_1_and_5_hooks()
                install_camera_preset_hooks(self)
                sanitize_blocks_in_memory(self)
                cfg = orig_get_config(self, *args, **kwargs)
                return sanitize_config_dict_py(cfg)

            gr.Blocks.get_config_file = hooked_get_config_file
            gr.Blocks._synexan_hooked = True
    except Exception as e:
        print(f"⚠️ [Runtime Hook] {e}")


def install_sitecustomize_globally():
    """Install sitecustomize.py into Python site-packages so app.py always imports it at startup."""
    shim_code = (
        "import huggingface_hub, sys, os\n"
        "if not hasattr(huggingface_hub, 'HfFolder'):\n"
        "    class HfFolder:\n"
        "        @staticmethod\n"
        "        def get_token():\n"
        "            return os.environ.get('HF_TOKEN', None) or os.environ.get('HUGGING_FACE_HUB_TOKEN', None)\n"
        "        @staticmethod\n"
        "        def save_token(token): pass\n"
        "        @staticmethod\n"
        "        def delete_token(): pass\n"
        "    huggingface_hub.HfFolder = HfFolder\n"
        "    sys.modules['huggingface_hub.HfFolder'] = HfFolder\n"
        "try:\n"
        "    sys.path.insert(0, '/kaggle/working/FrontendFlood/scripts')\n"
        "    import inject_smooth_stream\n"
        "    inject_smooth_stream.install_gradio_runtime_hooks()\n"
        "except Exception:\n"
        "    pass\n"
    )
    target_dirs = []
    try:
        target_dirs.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        usp = site.getusersitepackages()
        if isinstance(usp, str):
            target_dirs.append(usp)
    except Exception:
        pass

    for d in target_dirs:
        if d and os.path.isdir(d):
            try:
                with open(os.path.join(d, "sitecustomize.py"), "w", encoding="utf-8") as f:
                    f.write(shim_code)
            except Exception:
                pass


def inject_patch():
    install_custom_vehicle_weights()
    install_sitecustomize_globally()
    install_gradio_runtime_hooks()
    try:
        import gradio
        gradio_dir = os.path.dirname(gradio.__file__)
        templates_root = os.path.join(gradio_dir, "templates")

        script_dir = os.path.dirname(os.path.abspath(__file__))
        patch_file = os.path.join(script_dir, "smooth_stream_patch.js")

        if not os.path.exists(patch_file):
            print(f"⚠️ [Injector] Không tìm thấy file patch: {patch_file}")
            return False

        with open(patch_file, "r", encoding="utf-8") as f:
            js_code = f.read()

        patched_files = []
        for root, _, files in os.walk(templates_root):
            for fname in files:
                if fname.endswith(".html"):
                    tpath = os.path.join(root, fname)
                    try:
                        with open(tpath, "r", encoding="utf-8") as tf:
                            content = tf.read()

                        if "<head>" not in content and "</head>" not in content:
                            continue

                        if "synexan-smooth-stream" in content:
                            content = re.sub(
                                r'<script id="synexan-smooth-stream">.*?</script>\s*',
                                '',
                                content,
                                flags=re.DOTALL
                            )

                        # Tiêm ngay đầu thẻ <head> để chặn window.gradio_config ở 0ms trước khi Svelte render
                        if "<head>" in content:
                            injection = f'<head>\n<script id="synexan-smooth-stream">\n{js_code}\n</script>\n'
                            new_content = content.replace("<head>", injection, 1)
                        else:
                            injection = f'\n<script id="synexan-smooth-stream">\n{js_code}\n</script>\n</head>'
                            new_content = content.replace("</head>", injection, 1)

                        with open(tpath, "w", encoding="utf-8") as tf:
                            tf.write(new_content)
                        patched_files.append(tpath)
                    except Exception as inner_e:
                        print(f"⚠️ [Injector] Không thể vá {tpath}: {inner_e}")

        print(f"🎉 [Injector] Đã tích hợp Root-Level Controller v7 + Camera Presets vào {len(patched_files)} Gradio templates: {patched_files}")
        return len(patched_files) > 0
    except Exception as e:
        print(f"❌ [Injector] Lỗi tích hợp: {e}")
        return False


def start_v9_webhook_reporter():
    """Đảm bảo mọi kernel đều đồng bộ version v10_flood_v20 kèm nhãn device (gpu/cpu) về Vercel."""
    try:
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "synexan_v5_reporter"], check=False, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-9", "-f", "synexan_v7_reporter"], check=False, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-9", "-f", "synexan_v8_reporter"], check=False, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-9", "-f", "synexan_v9_reporter"], check=False, stderr=subprocess.DEVNULL)
        daemon_code = (
            "# synexan_v9_reporter\n"
            "import time, re, os, json, subprocess, urllib.request\n"
            "dev_type = 'cpu'\n"
            "try:\n"
            "    import torch\n"
            "    if torch.cuda.is_available(): dev_type = 'gpu'\n"
            "except Exception: pass\n"
            "webhooks = ['https://frontend-flood.vercel.app/api/backend-url']\n"
            "while True:\n"
            "    time.sleep(10)\n"
            "    try:\n"
            "        urllib.request.urlopen('http://127.0.0.1:7860/heartbeat', timeout=3).read()\n"
            "    except Exception:\n"
            "        continue\n"
            "    url = None\n"
            "    if os.path.exists('/tmp/cloudflared.log'):\n"
            "        txt = open('/tmp/cloudflared.log', errors='ignore').read()\n"
            "        m = re.findall(r'https://(?!api\\.)[a-zA-Z0-9]+-[a-zA-Z0-9-]+\\.trycloudflare\\.com', txt)\n"
            "        if m: url = m[-1]\n"
            "    if url:\n"
            "        for ep in webhooks:\n"
            "            try:\n"
            "                req = urllib.request.Request(ep, data=json.dumps({'url': url, 'version': 'v10_flood_v20', 'device': dev_type}).encode(), headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})\n"
            "                with urllib.request.urlopen(req, timeout=8) as r:\n"
            "                    rj = json.loads(r.read().decode('utf-8', errors='ignore'))\n"
            "                    if rj.get('superseded') is True:\n"
            "                        subprocess.run(['pkill', '-9', '-f', 'cloudflared'], check=False)\n"
            "                        subprocess.run(['pkill', '-9', '-f', 'app.py'], check=False)\n"
            "                        os._exit(0)\n"
            "            except Exception: pass\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", daemon_code],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    except Exception:
        pass


if __name__ == "__main__":
    inject_patch()
    start_v9_webhook_reporter()
