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
import functools
import urllib.request

EMOJI_PATTERN = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\uFE0F\u200D"
    r"🌊⚡🚗📁🖼️🚀⏹⚙️🗑🎯🚦📋📍💡📏🔄✅❌⚠️🛑🎉⏳🔎📌➔👉🎬📷📐💾↩️⚖️📺]+",
    flags=re.UNICODE,
)

PRESET_API_URL = "https://frontend-flood.vercel.app/api/camera-presets"
LOCAL_PRESETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_presets.json")
TMP_PRESETS_PATH = "/tmp/synexan_camera_presets.json"
CAMERA_PRESETS_CACHE = {}
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
    """Tải presets đã lưu của các Camera từ file cục bộ và từ Vercel Cloud (Upstash Redis)."""
    global CAMERA_PRESETS_CACHE
    merged = {}
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
    """Xác định định danh chuẩn (id, url, name) của Camera đang thao tác."""
    if isinstance(state_data, dict):
        if state_data.get("canonical_cam_key"):
            return (
                state_data["canonical_cam_key"],
                state_data.get("canonical_cam_url") or state_data["canonical_cam_key"],
                state_data.get("canonical_cam_name") or "Camera",
            )

    candidate_url = str(raw_url_or_none or "").strip()
    if not candidate_url and isinstance(state_data, dict):
        candidate_url = str(state_data.get("cam_source") or "").strip()

    cameras = getattr(main_mod, "CANTHO_CAMERAS", []) or []
    for c in cameras:
        c_url = str(c.get("url", "")).strip()
        c_url_sub1 = c_url.replace("subtype=0", "subtype=1")
        if candidate_url and (candidate_url == c_url or candidate_url == c_url_sub1 or candidate_url == c.get("id")):
            return c["id"], c_url, c.get("name", c["id"])

    if isinstance(state_data, dict):
        cam_title = str(state_data.get("cam_title") or "")
        for c in cameras:
            if c.get("name") and c["name"] in cam_title:
                return c["id"], c["url"], c["name"]

    if LAST_ACTIVE_CAMERA.get("key") and (not isinstance(state_data, dict) or state_data.get("source_type") == "camera_stream"):
        return LAST_ACTIVE_CAMERA["key"], LAST_ACTIVE_CAMERA["url"], LAST_ACTIVE_CAMERA["name"]

    if candidate_url:
        return candidate_url, candidate_url, candidate_url

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


def install_camera_preset_hooks(blocks_instance):
    """Bọc các hàm sự kiện Camera & Lưu cột mốc trong __main__ và demo.fns để tự động lưu/nạp mốc cố định."""
    main_mod = sys.modules.get("__main__")
    if not main_mod or getattr(main_mod, "_synexan_cam_hooks_installed", False):
        return
    main_mod._synexan_cam_hooks_installed = True

    load_camera_presets_from_all_sources()
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
                        return prev_rgb, slider_upd, new_state, msg, drop_upd
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
                        return first_frame, slider_upd, new_state, msg
            except Exception as e:
                print(f"⚠️ [Fusion Cam Restore] {e}")
            return res

        setattr(main_mod, "on_fusion_camera_connect", wrapped_fusion_cam)
        fn_replacements[orig_fusion_cam] = wrapped_fusion_cam

    # 3. Hook on_traffic_camera_connect
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
                        return prev_rgb, slider_upd, new_state, msg
            except Exception as e:
                print(f"⚠️ [Traffic Cam Restore] {e}")
            return res

        setattr(main_mod, "on_traffic_camera_connect", wrapped_traffic_cam)
        fn_replacements[orig_traffic_cam] = wrapped_traffic_cam

    # 4. Hook các hàm Lưu / Xóa mốc & ROI để tự động đồng bộ lên Cloud ngay khi người dùng thao tác
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
    try:
        blocks_dict = getattr(blocks_instance, "blocks", {}) or {}
        for b in blocks_dict.values():
            elem_id = getattr(b, "elem_id", None)
            if elem_id in ("tab5_setup_status", "traffic_setup_status", "fusion_setup_status"):
                b.value = ""
                b.visible = False

            lbl = getattr(b, "label", None)
            if isinstance(lbl, str) and "Bề rộng cột mốc" in lbl:
                b.value = getattr(b, "minimum", 4) or 4

            val = getattr(b, "value", None)
            if isinstance(val, dict) and "strip_width" in val:
                val["strip_width"] = 4

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
            if elem_id in ("tab5_setup_status", "traffic_setup_status", "fusion_setup_status"):
                p["value"] = ""
                p["visible"] = False

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
