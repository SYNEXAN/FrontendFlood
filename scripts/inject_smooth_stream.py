#!/usr/bin/env python3
"""
Synexan AI Root-Level UI & Smooth Stream Injector (v5_root_clean)
1. Installs sitecustomize.py into Python site-packages so app.py ALWAYS loads
   install_gradio_runtime_hooks() at Python startup before Gradio initializes.
2. Hooks gr.Blocks.get_config_file (without altering Component.__init__ signatures
   used by Gradio's ComponentMeta) so all block objects AND config JSON have:
   - 0 emojis/icons anywhere
   - tab5_setup_status / traffic_setup_status / fusion_setup_status emptied & hidden
   - "Bề rộng cột mốc" slider value=4 and state strip_width=4
3. Injects smooth_stream_patch.js right at the top of <head> in all Gradio HTML templates
   (index.html and share.html) so Pre-Render sanitization runs at 0ms with zero flash on reload.
4. Ensures 100% zero modifications to the backend/ folder.
"""
import os
import sys
import re
import site

EMOJI_PATTERN = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\uFE0F\u200D"
    r"🌊⚡🚗📁🖼️🚀⏹⚙️🗑🎯🚦📋📍💡📏🔄✅❌⚠️🛑🎉⏳🔎📌➔👉🎬📷📐]+",
    flags=re.UNICODE,
)


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
    cleaned = re.sub(r"(#+\s*)\s+", r"\1", cleaned)
    cleaned = re.sub(r">\s+", ">", cleaned)
    return cleaned.lstrip()


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


def install_gradio_runtime_hooks():
    """Hook gr.Blocks.get_config_file without modifying Component.__init__ signatures."""
    try:
        import gradio as gr

        if not getattr(gr.Blocks, "_synexan_hooked", False):
            orig_get_config = gr.Blocks.get_config_file

            def hooked_get_config_file(self, *args, **kwargs):
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

        print(f"🎉 [Injector] Đã tích hợp Root-Level Controller v5 vào {len(patched_files)} Gradio templates: {patched_files}")
        return len(patched_files) > 0
    except Exception as e:
        print(f"❌ [Injector] Lỗi tích hợp: {e}")
        return False


def start_v5_webhook_reporter():
    """Đảm bảo mọi kernel (kể cả kernel đang chạy vòng lặp launcher cũ) đều đồng bộ version v5_root_clean về Vercel."""
    try:
        import subprocess
        subprocess.run(["pkill", "-9", "-f", "synexan_v5_reporter"], check=False, stderr=subprocess.DEVNULL)
        daemon_code = (
            "# synexan_v5_reporter\n"
            "import time, re, os, json, urllib.request\n"
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
            "                req = urllib.request.Request(ep, data=json.dumps({'url': url, 'version': 'v6_intact_props'}).encode(), headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})\n"
            "                urllib.request.urlopen(req, timeout=8)\n"
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
    start_v5_webhook_reporter()
