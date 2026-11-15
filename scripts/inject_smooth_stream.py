#!/usr/bin/env python3
"""
Synexan AI Smooth Stream Injector
Injects the client-side Jitter Buffer script into Gradio templates.
Ensures 100% zero changes to the backend/ folder.
"""
import os
import sys
import re

def inject_patch():
    try:
        import gradio
        gradio_dir = os.path.dirname(gradio.__file__)
        template_file = os.path.join(gradio_dir, "templates", "frontend", "index.html")
        
        if not os.path.exists(template_file):
            print(f"⚠️ [Injector] Không tìm thấy Gradio template: {template_file}")
            return False

        with open(template_file, "r", encoding="utf-8") as f:
            content = f.read()

        # Đọc nội dung script patch mới nhất
        script_dir = os.path.dirname(os.path.abspath(__file__))
        patch_file = os.path.join(script_dir, "smooth_stream_patch.js")
        
        if not os.path.exists(patch_file):
            print(f"⚠️ [Injector] Không tìm thấy file patch: {patch_file}")
            return False

        with open(patch_file, "r", encoding="utf-8") as f:
            js_code = f.read()

        # Nếu đã có script cũ, xóa khối cũ để luôn cập nhật bộ code mới nhất
        if "synexan-smooth-stream" in content:
            content = re.sub(r'<script id="synexan-smooth-stream">.*?</script>\s*', '', content, flags=re.DOTALL)
            print("🔄 [Injector] Đã phát hiện script cũ, tiến hành cập nhật phiên bản mới nhất...")

        injection = f"\n<script id=\"synexan-smooth-stream\">\n{js_code}\n</script>\n</head>"
        new_content = content.replace("</head>", injection, 1)

        with open(template_file, "w", encoding="utf-8") as f:
            f.write(new_content)

        print(f"🎉 [Injector] Đã tích hợp thành công GPU Canvas & Single-Screen Manager vào Gradio template ({template_file})!")
        return True
    except Exception as e:
        print(f"❌ [Injector] Lỗi tích hợp: {e}")
        return False

if __name__ == "__main__":
    inject_patch()
