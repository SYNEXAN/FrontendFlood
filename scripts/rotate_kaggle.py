#!/usr/bin/env python3
"""
scripts/rotate_kaggle.py
Tự động xoay vòng tài khoản Kaggle thông minh (Smart Low Quota Rotation)
- Đọc danh sách tài khoản từ secret KAGGLE_ACCOUNTS_JSON
- Chỉ xoay khi tài khoản hiện tại gần hết giờ (< 1.0h) hoặc bị Quota Exceeded
- Giữ nguyên phiên nếu kernel đang RUNNING hoặc QUEUED
- An toàn tuyệt đối, không lộ credentials
"""

import os
import sys
import json
import subprocess
import ast
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
import csv

# Ngưỡng an toàn tối thiểu (giờ GPU) trước khi quyết định xoay sang tài khoản mới
MIN_GPU_HOURS_THRESHOLD = float(os.environ.get("MIN_GPU_HOURS_THRESHOLD", "1.0"))
FORCE_PUSH = os.environ.get("FORCE_PUSH", "false").lower() in ("true", "1", "yes") or "--force" in sys.argv
KERNEL_SLUG = "synexan-flood-backend-gpu"
FRONTEND_API_URL = os.environ.get("FRONTEND_API_URL", "https://frontend-flood.vercel.app/api/kernel-log")
import shutil

# Khởi tạo lệnh Kaggle CLI an toàn tuyệt đối trên mọi nền tảng (Linux & Windows)
KAGGLE_BIN = shutil.which("kaggle")
if KAGGLE_BIN and os.name != "nt":
    KAGGLE_CMD = [KAGGLE_BIN]
else:
    KAGGLE_CMD = [sys.executable, "-c", "from kaggle.cli import main; main()"]


def report_kernel_state(status, message, details=None, account=None):
    """Gửi telemetry log trạng thái Kaggle kernel về Vercel frontend để người dùng theo dõi trực tiếp."""
    try:
        payload = {
            "status": status,  # "queued", "running", "pushing", "error", "success", "limit"
            "message": message,
            "details": str(details) if details else None,
            "account": account
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            FRONTEND_API_URL,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
    except Exception as e:
        print(f"   [Telemetry] Không thể gửi log tới frontend: {e}")


def resolve_username_from_token(token, fallback_index=None):
    """Tự động xác thực trực tiếp từ mã API Token (KGAT_...) qua Kaggle SDK mà không cần khai báo username."""
    if not token:
        return None
    # 1. Dùng trực tiếp hàm _introspect_token chính thức của KaggleApi SDK
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
        api = KaggleApi()
        owner = api._introspect_token(token)
        if owner and isinstance(owner, str) and owner.strip():
            return owner.strip()
    except Exception as e:
        print(f"   [Token Introspect] Thử phương thức dự phòng ({e})")

    # 2. Dự phòng qua Kaggle CLI (--mine) với biến môi trường KAGGLE_API_TOKEN
    try:
        env = os.environ.copy()
        env["KAGGLE_API_TOKEN"] = token
        proc = subprocess.run(
            [*KAGGLE_CMD, "kernels", "list", "--mine", "--page-size", "5", "-v"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15
        )
        lines = proc.stdout.strip().splitlines()
        if len(lines) > 1:
            reader = csv.DictReader(lines)
            for row in reader:
                ref = (row.get("ref") or "").strip()
                if "/" in ref:
                    return ref.split("/")[0].strip()
    except Exception:
        pass
    return None


def load_accounts():
    """Tải toàn bộ danh sách mã API (KGAT_...) hoặc key từ KAGGLE_ACCOUNTS_JSON ở mọi định dạng."""
    raw_json = os.environ.get("KAGGLE_ACCOUNTS_JSON", "").strip()
    extra_key = os.environ.get("KAGGLE_KEY", "").strip()
    extra_user = os.environ.get("KAGGLE_USERNAME", "").strip()

    candidates = []
    seen_tokens = set()

    # 1. Trích xuất trực tiếp toàn bộ mã KGAT_... xuất hiện trong KAGGLE_ACCOUNTS_JSON và KAGGLE_KEY
    for src_text in (raw_json, extra_key):
        if src_text:
            for tok in re.findall(r"KGAT_[a-zA-Z0-9_\-]+", src_text):
                if tok not in seen_tokens:
                    seen_tokens.add(tok)
                    candidates.append({"username": "", "key": tok, "token": tok})

    # 2. Hỗ trợ thêm các cặp {username, key} truyền thống (nếu không phải KGAT_...)
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            items = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, dict) else [])
            for it in items:
                if isinstance(it, dict):
                    u = str(it.get("username") or "").strip()
                    k = str(it.get("key") or it.get("token") or "").strip()
                    if k and k not in seen_tokens:
                        seen_tokens.add(k)
                        candidates.append({"username": u, "key": k, "token": k if k.startswith("KGAT_") else ""})
        except Exception:
            pass

    if extra_user and extra_key and extra_key not in seen_tokens:
        seen_tokens.add(extra_key)
        candidates.append({
            "username": extra_user,
            "key": extra_key,
            "token": extra_key if extra_key.startswith("KGAT_") else ""
        })

    # 3. Tự động quét thêm các tài khoản MCP Kaggle tại ~/.kaggle_accounts/*/kaggle.json (nếu có)
    mcp_accounts_dir = Path.home() / ".kaggle_accounts"
    if mcp_accounts_dir.exists() and mcp_accounts_dir.is_dir():
        for acc_file in sorted(mcp_accounts_dir.glob("*/kaggle.json")):
            try:
                with open(acc_file, "r", encoding="utf-8") as mf:
                    md = json.load(mf)
                u = str(md.get("username") or "").strip()
                k = str(md.get("key") or md.get("token") or "").strip()
                if k and k not in seen_tokens:
                    seen_tokens.add(k)
                    candidates.append({
                        "username": u,
                        "key": k,
                        "token": k if k.startswith("KGAT_") else ""
                    })
            except Exception:
                pass

    print(f"🔎 Phát hiện tổng cộng {len(candidates)} mã API Kaggle trong danh sách xoay vòng.")

    resolved = []
    for idx, item in enumerate(candidates):
        u = item.get("username", "").strip()
        k = item.get("key", "").strip()
        tok = item.get("token", "").strip() or (k if k.startswith("KGAT_") else "")

        if not u and tok:
            discovered_user = resolve_username_from_token(tok, fallback_index=idx)
            if discovered_user:
                print(f"✅ Đã xác thực thành công mã API Token #{idx + 1}")
                u = discovered_user
            else:
                print(f"⚠️ Mã API Token #{idx + 1} không hợp lệ hoặc đã hết hạn.")

        if u and (k or tok):
            resolved.append({"username": u, "key": k or tok, "token": tok})

    return resolved


def setup_kaggle_credentials(username, key, token=None):
    """Cấu hình credentials vào ~/.kaggle/kaggle.json an toàn."""
    kaggle_dir = Path.home() / ".kaggle"
    kaggle_dir.mkdir(parents=True, exist_ok=True)
    kaggle_file = kaggle_dir / "kaggle.json"

    data = {"username": username}
    if token:
        data["token"] = token
        os.environ["KAGGLE_API_TOKEN"] = token
    else:
        os.environ.pop("KAGGLE_API_TOKEN", None)

    if key:
        data["key"] = key
        os.environ["KAGGLE_KEY"] = key
    else:
        os.environ.pop("KAGGLE_KEY", None)

    os.environ["KAGGLE_USERNAME"] = username

    with open(kaggle_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    try:
        os.chmod(kaggle_file, 0o600)
    except Exception:
        pass

    os.environ["KAGGLE_USERNAME"] = username


def get_remaining_gpu_hours():
    """Truy vấn quota Kaggle hiện tại (nếu Kaggle API hỗ trợ)."""
    # Kaggle CLI hiện tại không cung cấp lệnh `kaggle quota`, trả về None an toàn
    return None


def generate_unique_slug():
    """Tạo slug độc nhất cho notebook mới toanh mỗi lần chạy backend."""
    now_str = datetime.utcnow().strftime("%m%d%H%M%S")
    return f"synexan-flood-gpu-{now_str}"


def get_exact_kernel_status(username, slug=KERNEL_SLUG):
    """
    Truy vấn trạng thái chính xác của kernel slug.
    Trả về: 'RUNNING', 'QUEUED', 'COMPLETE', 'ERROR', hoặc 'UNKNOWN'.
    """
    target_id = f"{username}/{slug}"
    try:
        proc = subprocess.run(
            [*KAGGLE_CMD, "kernels", "status", target_id],
            capture_output=True,
            text=True,
            timeout=20
        )
        combined = (proc.stdout + proc.stderr).strip()
        lower = combined.lower()

        # Xử lý an toàn khi gặp Kaggle API Rate Limit 429
        if "429" in lower or "too many requests" in lower:
            print("   ⚠️ [Rate Limit] Kaggle API 429 Too Many Requests. Tạm nghỉ 12s...")
            time.sleep(12)
            proc2 = subprocess.run(
                [*KAGGLE_CMD, "kernels", "status", target_id],
                capture_output=True,
                text=True,
                timeout=20
            )
            combined = (proc2.stdout + proc2.stderr).strip()
            lower = combined.lower()

        if "kernelworkerstatus.running" in lower or '"running"' in lower:
            return "RUNNING", combined
        elif "kernelworkerstatus.queued" in lower or '"queued"' in lower:
            return "QUEUED", combined
        elif "kernelworkerstatus.complete" in lower or '"complete"' in lower:
            return "COMPLETE", combined
        elif "kernelworkerstatus.error" in lower or '"error"' in lower:
            return "ERROR", combined
        elif "cancel" in lower:
            return "CANCELED", combined
        return "UNKNOWN", combined
    except Exception as e:
        return "ERROR", str(e)


def get_slug_age_seconds(slug):
    """Tính tuổi (theo giây) của notebook slug dạng synexan-flood-gpu-MMDDHHMMSS so với UTC hiện tại."""
    try:
        match = re.search(r"(\d{10})$", slug)
        if match:
            digits = match.group(1)
            now_utc = datetime.now(timezone.utc)
            kernel_dt = datetime(
                year=now_utc.year,
                month=int(digits[0:2]),
                day=int(digits[2:4]),
                hour=int(digits[4:6]),
                minute=int(digits[6:8]),
                second=int(digits[8:10]),
                tzinfo=timezone.utc
            )
            return max(0, (now_utc - kernel_dt).total_seconds())
    except Exception:
        pass
    return None


def find_active_backend(username):
    """
    Kiểm tra xem có bất kỳ notebook synexan-* nào của username đang RUNNING hoặc QUEUED hay không.
    Sử dụng '-s synexan -v' để lọc chính xác và phân tích qua CSV parser chuẩn.
    Tự động hủy và dọn dẹp các phiên QUEUED thây ma (đã kẹt > 3 phút).
    """
    try:
        proc = subprocess.run(
            [*KAGGLE_CMD, "kernels", "list", "--mine", "--page-size", "50", "-v"],
            capture_output=True,
            text=True,
            timeout=20
        )
        lines = proc.stdout.strip().splitlines()
        first_queued = None

        if len(lines) > 1:
            reader = csv.DictReader(lines)
            for row in reader:
                kernel_ref = row.get("ref", "").strip()
                if not kernel_ref:
                    continue
                if username.lower() in kernel_ref.lower() and "synexan" in kernel_ref.lower():
                    time.sleep(1) # Tôn trọng Kaggle rate limit
                    st_proc = subprocess.run(
                        [*KAGGLE_CMD, "kernels", "status", kernel_ref],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    st_out = (st_proc.stdout + st_proc.stderr).strip()
                    lower_st = st_out.lower()
                    if "429" in lower_st or "too many requests" in lower_st:
                        time.sleep(12)
                        st_proc2 = subprocess.run([*KAGGLE_CMD, "kernels", "status", kernel_ref], capture_output=True, text=True, timeout=10)
                        st_out = (st_proc2.stdout + st_proc2.stderr).strip()
                        lower_st = st_out.lower()

                    if "kernelworkerstatus.running" in lower_st or '"running"' in lower_st:
                        age = get_slug_age_seconds(kernel_ref)
                        if age is not None and age > 300:
                            print(f"   🛑 Phiên RUNNING '{kernel_ref}' đã tạo cách đây {int(age // 60)} phút nhưng mất kết nối Tunnel. Đang xóa ngay để giải phóng slot GPU...")
                            subprocess.run([*KAGGLE_CMD, "kernels", "delete", "-y", kernel_ref], capture_output=True, text=True, timeout=12)
                            time.sleep(2.5)
                            continue
                        return kernel_ref, "RUNNING", st_out
                    elif "kernelworkerstatus.queued" in lower_st or '"queued"' in lower_st:
                        # Kiểm tra xem phiên QUEUED này có phải zombie queue (cũ > 3 phút)
                        age = get_slug_age_seconds(kernel_ref)
                        if age is not None and age > 180:
                            print(f"   🛑 Phiên QUEUED '{kernel_ref}' đã kẹt {int(age // 60)} phút trước (zombie queue). Đang xóa để giải phóng GPU slot...")
                            subprocess.run([*KAGGLE_CMD, "kernels", "delete", "-y", kernel_ref], capture_output=True, text=True, timeout=10)
                            time.sleep(4)
                            continue
                        elif not first_queued and not kernel_ref.lower().endswith("synexan-flood-backend-gpu"):
                            first_queued = (kernel_ref, "QUEUED", st_out)

        if first_queued:
            return first_queued

    except Exception as e:
        print(f"   [find_active_backend] Lỗi: {e}")

    return None, "NONE", None


def cleanup_stale_kernels(username, current_slug=None, force_kill_running=False):
    """
    Tự động dọn dẹp các notebook synexan-* cũ để giải phóng slot GPU.
    Khi force_kill_running=True hoặc FORCE_PUSH=True, xóa luôn các phiên synexan-* đang RUNNING/QUEUED cũ.
    Trả về số lượng kernel đã xóa.
    """
    deleted_count = 0
    print(f"🧹 [Auto-Cleanup] Quét và dọn dẹp các notebook synexan cũ (force_kill={force_kill_running or FORCE_PUSH})...")
    try:
        proc = subprocess.run(
            [*KAGGLE_CMD, "kernels", "list", "--mine", "--page-size", "50", "-v"],
            capture_output=True,
            text=True,
            timeout=20
        )
        lines = proc.stdout.strip().splitlines()
        if len(lines) > 1:
            reader = csv.DictReader(lines)
            checked_synexan = 0
            for row in reader:
                kernel_ref = row.get("ref", "").strip()
                if not kernel_ref:
                    continue
                if "synexan" in kernel_ref.lower():
                    if current_slug and current_slug.lower() in kernel_ref.lower():
                        continue
                    checked_synexan += 1
                    if checked_synexan > 8:
                        break

                    st_proc = subprocess.run(
                        [*KAGGLE_CMD, "kernels", "status", kernel_ref],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    st_out = (st_proc.stdout + st_proc.stderr).lower()
                    is_active = ("running" in st_out) or ("queued" in st_out)

                    if is_active and (force_kill_running or FORCE_PUSH):
                        print(f"   🗑️ Đang xóa phiên synexan đang chạy để giải phóng slot GPU: {kernel_ref}")
                        res_del = subprocess.run([*KAGGLE_CMD, "kernels", "delete", "-y", kernel_ref], capture_output=True, text=True, timeout=15)
                        if res_del.returncode != 0:
                            try:
                                from kaggle.api.kaggle_api_extended import KaggleApi
                                api_del = KaggleApi()
                                api_del.authenticate()
                                api_del.kernels_delete(kernel_ref, no_confirm=True)
                            except Exception as de:
                                print(f"      ⚠️ Lỗi xóa {kernel_ref}: {de}")
                        deleted_count += 1
                        time.sleep(2.5)
                        continue

                    if kernel_ref.lower().endswith("/synexan-flood-backend-gpu"):
                        continue

                    age = get_slug_age_seconds(kernel_ref)
                    is_zombie_queue = ("queued" in st_out) and (age is not None and age > 180)

                    if is_zombie_queue or (("complete" in st_out or "error" in st_out or "cancel" in st_out) and deleted_count < 3):
                        print(f"   🗑️ Đang xóa kernel synexan cũ/kẹt: {kernel_ref}")
                        subprocess.run([*KAGGLE_CMD, "kernels", "delete", "-y", kernel_ref], capture_output=True, text=True, timeout=12)
                        deleted_count += 1
                        time.sleep(1.5)
        if deleted_count > 0:
            time.sleep(3)
    except Exception as e:
        print(f"   [Auto-Cleanup] Lỗi quét kernel: {e}")
    return deleted_count


def monitor_queue_and_retry(username, slug, token=None, timeout_seconds=105):
    """
    Giám sát hàng đợi của notebook trong tối đa timeout_seconds (105s).
    - Chu kỳ kiểm tra 15s/lần, vừa nhẹ nhàng với API vừa đủ thời gian để Kaggle cấp GPU container.
    - Nếu chuyển sang RUNNING: Thành công rực rỡ!
    """
    target_id = f"{username}/{slug}"
    print(f"⏳ Giám sát hàng đợi của '{target_id}' trong tối đa {timeout_seconds}s...")
    poll_interval = 15
    elapsed = 0

    while elapsed < timeout_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval

        status, raw_msg = get_exact_kernel_status(username, slug)
        print(f"   [{elapsed}s/{timeout_seconds}s] Trạng thái của '{target_id}': {status}")

        if status == "RUNNING":
            print(f"🎉 [CẤP GPU THÀNH CÔNG] Notebook '{target_id}' đã chuyển sang RUNNING sau {elapsed}s!")
            report_kernel_state("running", f"Kaggle đã cấp GPU cho '{slug}' sau {elapsed}s! Đang nạp mô hình AI...", raw_msg, username)
            return True, "RUNNING"

        kaggle_url = f"https://www.kaggle.com/code/{username}/{slug}"
        report_kernel_state("queued", f"Kaggle đang xếp hàng tài nguyên cho '{slug}' ({elapsed}s/{timeout_seconds}s)...", kaggle_url, username)

    print(f"ℹ️ Notebook '{target_id}' vẫn đang ở trong hàng đợi QUEUED sau {timeout_seconds}s.")
    return False, "QUEUED"


def update_kernel_metadata(username, slug, use_gpu=True, machine_shape="NvidiaTeslaT4"):
    """Cập nhật ID, title và cấu hình accelerator GPU/CPU trong notebooks/kernel-metadata.json."""
    meta_path = Path("notebooks") / "kernel-metadata.json"
    if not meta_path.exists():
        print(f"❌ Không tìm thấy {meta_path}!")
        return False

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        meta["id"] = f"{username}/{slug}"
        meta["title"] = slug
        meta["enable_gpu"] = use_gpu
        if use_gpu and machine_shape:
            meta["machine_shape"] = machine_shape
        else:
            meta.pop("machine_shape", None)
        meta["enable_internet"] = True
        meta["is_private"] = False  # Chế độ Public để mọi tài khoản đều truy cập và chạy thủ công được!

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        return True
    except Exception as e:
        print(f"❌ Lỗi cập nhật kernel-metadata.json: {e}")
        return False


def push_kernel():
    """Thực thi đẩy notebook lên Kaggle GPU (cấu hình machine_shape trong kernel-metadata.json)."""
    try:
        proc = subprocess.run(
            [*KAGGLE_CMD, "kernels", "push", "-p", "notebooks"],
            capture_output=True,
            text=True,
            timeout=45
        )
        combined_output = proc.stdout + proc.stderr
        print(f"   [Kaggle Push Raw Output]: {combined_output.strip()}")
        lower_out = combined_output.lower()
        is_success = proc.returncode == 0 and ("successfully pushed" in lower_out or "configuration pushed" in lower_out)
        return is_success, combined_output.strip()
    except Exception as e:
        return False, str(e)


def verify_backend_health(url, timeout=5):
    """
    Kiểm tra trực tiếp URL backend có đang thực sự phản hồi 200 OK hay không.
    Trả về (is_alive, is_warming_up, details).
    - is_alive = True nếu /heartbeat hoặc /config trả về HTTP 200.
    - is_warming_up = True nếu gặp HTTP 530, 502, 504 (tunnel mở nhưng local port 7860 đang nạp weights).
    - is_alive = False nếu gặp 1033 (tunnel cũ đã hết hạn) hoặc lỗi khác.
    """
    if not url or not url.startswith("http"):
        return False, False, "URL không hợp lệ"

    clean_url = url.rstrip("/")
    for endpoint in ["/heartbeat", "/config"]:
        check_url = f"{clean_url}{endpoint}"
        try:
            req = urllib.request.Request(
                check_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return True, False, f"HTTP 200 OK tại {endpoint}"
        except urllib.error.HTTPError as he:
            if he.code in [530, 502, 504]:
                return False, True, f"HTTP {he.code} (Gradio đang nạp mô hình vào GPU)"
            elif he.code in [1033]:
                return False, False, f"HTTP {he.code} (Tunnel cũ đã hết hạn)"
        except Exception as e:
            err_str = str(e)
            if "530" in err_str:
                return False, True, "Cloudflare 530 (Đang khởi động)"
    return False, False, "Chưa nhận phản hồi từ backend"


def wait_for_live_backend_and_test(username, slug, timeout_seconds=300):
    """
    Sau khi notebook chuyển sang RUNNING trên Kaggle:
    Chờ Gradio nạp mô hình, mở port 7860 và phát tán Cloudflare URL về Vercel.
    Khi URL xuất hiện và được xác thực phản hồi 200 OK, tự động chạy test suy luận AI (Inference Test)!
    """
    print(f"\n======================================================================")
    print(f"⏳ [SUPERVISOR] Chờ Backend GPU ({username}/{slug}) nạp mô hình & phát tán URL (tối đa {timeout_seconds}s)...")
    print(f"======================================================================")

    # 1. Ghi nhận URL ban đầu trên Vercel để tránh nhận nhầm URL cũ (stale cache)
    initial_url = ""
    try:
        req_init = urllib.request.Request(
            "https://frontend-flood.vercel.app/api/config",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req_init, timeout=6) as resp:
            d_init = json.loads(resp.read().decode("utf-8"))
            initial_url = d_init.get("defaultBackendUrl", "").strip()
            print(f"📌 [Supervisor] URL hiện có trên Vercel trước khi khởi động: '{initial_url}'")
    except Exception as e:
        print(f"⚠️ [Supervisor] Không đọc được initial URL: {e}")

    elapsed = 0
    poll_gap = 10

    while elapsed < timeout_seconds:
        time.sleep(poll_gap)
        elapsed += poll_gap
        try:
            req = urllib.request.Request(
                "https://frontend-flood.vercel.app/api/config",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                live_url = data.get("defaultBackendUrl", "").strip()
                k_log = data.get("kernelLog") or {}
                k_status = k_log.get("status")
                k_msg = k_log.get("message", "")
                k_details = (k_log.get("details") or "").strip()

                print(f"   [{elapsed}s/{timeout_seconds}s] Vercel Status: {k_status} | Msg: {k_msg}")

                candidate_url = None
                if k_details and k_details.startswith("http") and "kaggle.com" not in k_details:
                    candidate_url = k_details
                elif live_url and live_url.startswith("http") and "kaggle.com" not in live_url:
                    if live_url != initial_url or k_status == "ready":
                        candidate_url = live_url

                if candidate_url:
                    print(f"   🔎 Thẩm tra tính sẵn sàng của URL: {candidate_url}...")
                    is_alive, is_warming, probe_msg = verify_backend_health(candidate_url, timeout=5)

                    if is_alive:
                        print(f"\n🎉🎉🎉 [XÁC THỰC THÀNH CÔNG LIVE BACKEND URL]: {candidate_url} ({probe_msg}) 🎉🎉🎉")
                        report_kernel_state("ready", "Backend GPU đã sẵn sàng và kết nối thành công!", candidate_url, username)

                        # Chạy script test suy luận AI
                        print("🚀 Đang chạy script kiểm thử suy luận AI (Inference Test) thực tế...")
                        test_proc = subprocess.run(
                            [sys.executable, "scripts/test_backend_inference.py", "--url", candidate_url],
                            capture_output=True,
                            text=True,
                            timeout=120
                        )
                        print(test_proc.stdout)
                        if test_proc.stderr:
                            print(f"⚠️ [Logs phụ]: {test_proc.stderr}")

                        if test_proc.returncode == 0:
                            print("=" * 70)
                            print("✅ [HOÀN TẤT MỤC TIÊU 100%] Backend GPU hoạt động mượt mà và đã sẵn sàng phục vụ!")
                            print("=" * 70)
                            return True
                        else:
                            print(f"⚠️ Test suy luận trả về mã {test_proc.returncode}. Tiếp tục theo dõi...")
                    elif is_warming:
                        print(f"   ⏳ Backend đang khởi động ({probe_msg}). Vui lòng chờ...")
                    else:
                        print(f"   ⚠️ URL chưa sẵn sàng: {probe_msg}")

                if k_status == "error":
                    print(f"❌ [LỖI TỪ NOTEBOOK]: {k_msg}")
                    return False

        except Exception as e:
            print(f"   [Supervisor] Đang đợi Vercel API: {e}")

        # Định kỳ mỗi 30s kiểm tra trạng thái kernel trên Kaggle xem có bị lỗi ngoài ý muốn không
        if elapsed % 30 == 0:
            k_st, _ = get_exact_kernel_status(username, slug)
            if k_st in ["ERROR", "CANCELED"]:
                print(f"❌ [Kaggle Kernel] Phiên {username}/{slug} đã dừng với trạng thái: {k_st}")
                return False

    print("⚠️ Quá thời gian chờ URL trực tiếp. Phiên backend vẫn đang tiếp tục khởi chạy trên Kaggle.")
    return False


def main():
    print("=" * 70)
    print("🤖 SYNEXAN SMART KAGGLE ROTATION RUNNER (DYNAMIC NEW NOTEBOOK)")
    print("=" * 70)

    # 0. Kiểm tra trước xem đã có Backend GPU nào đang Online trên Vercel hay chưa (nếu đang là CPU thì tiếp tục tìm GPU để nâng cấp!)
    if not FORCE_PUSH:
        try:
            req_pre = urllib.request.Request(
                "https://frontend-flood.vercel.app/api/config",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req_pre, timeout=8) as resp:
                pre_data = json.loads(resp.read().decode("utf-8"))
                existing_url = (pre_data.get("defaultBackendUrl") or "").strip()
                k_log = pre_data.get("kernelLog") or {}
                cur_device = str(k_log.get("device") or "gpu").lower()
                if existing_url and existing_url.startswith("http") and "kaggle.com" not in existing_url:
                    is_alive, _, probe_msg = verify_backend_health(existing_url, timeout=6)
                    if is_alive and cur_device != "cpu":
                        print(f"🎉 [ALREADY ONLINE - GPU] Phát hiện Backend GPU đang hoạt động tại: {existing_url} ({probe_msg})")
                        print("👉 Giữ nguyên phiên GPU hiện tại, không đẩy thêm notebook mới.")
                        report_kernel_state("ready", "Backend GPU đang hoạt động ổn định!", existing_url, "active")
                        sys.exit(0)
                    elif is_alive and cur_device == "cpu":
                        print(f"⚡ [CPU -> GPU UPGRADE] Backend hiện tại ({existing_url}) đang chạy tạm bằng CPU. Tiến hành quét danh sách API để nâng cấp lên GPU Tesla T4!")
        except Exception as e:
            print(f"ℹ️ [Pre-check] Chưa xác nhận được backend online sẵn: {e}")

    accounts = load_accounts()
    if not accounts:
        print("❌ LỖI: Không tìm thấy bất kỳ mã API Kaggle nào trong KAGGLE_ACCOUNTS_JSON!")
        print("Vui lòng cấu hình Secret KAGGLE_ACCOUNTS_JSON trong Settings -> Secrets của GitHub.")
        sys.exit(1)

    if FORCE_PUSH and len(accounts) > 1:
        rev = list(reversed(accounts))
        offset = int(time.time() // 45) % len(rev)
        accounts = rev[offset:] + rev[:offset]

    print(f"📋 Đã nạp thành công {len(accounts)} mã API Kaggle từ GitHub Secret.")

    gpu_quota_exhausted = set()
    gpu_accelerators = [
        (True, "NvidiaTeslaT4", "GPU Tesla T4 (Dual T4)"),
        (True, None, "GPU Tesla P100 (Single GPU)"),
    ]

    # ==========================================================================
    # GIAI ĐOẠN 1: ƯU TIÊN TUYỆT ĐỐI GPU TRÊN TOÀN BỘ DANH SÁCH API (2 VÒNG QUÉT)
    # ==========================================================================
    MAX_GPU_ROUNDS = 2
    for round_idx in range(1, MAX_GPU_ROUNDS + 1):
        if len(gpu_quota_exhausted) >= len(accounts):
            print("⚠️ Tất cả các mã API trong danh sách đều đã báo hết 30h quota GPU/tuần!")
            break

        print("\n" + "=" * 70)
        print(f"🚀 [GIAI ĐOẠN 1 - VÒNG GPU {round_idx}/{MAX_GPU_ROUNDS}] Quét tìm & kích hoạt GPU trên danh sách API...")
        print("=" * 70)

        for idx, acc in enumerate(accounts, start=1):
            username = acc["username"]
            key = acc["key"]
            api_label = f"API #{idx}/{len(accounts)}"
            if username in gpu_quota_exhausted:
                print(f"⏭️ Bỏ qua {api_label} (đã hết 30h quota GPU/tuần).")
                continue

            print(f"\n🔍 [{api_label}] Đang kiểm tra tài nguyên GPU...")
            setup_kaggle_credentials(username, key, acc.get("token"))

            # Khi FORCE_PUSH = true -> Xóa ngay các phiên synexan cũ đang chiếm slot trên API này trước khi đẩy mới
            if FORCE_PUSH:
                cleanup_stale_kernels(username, force_kill_running=True)
            else:
                cleanup_stale_kernels(username, force_kill_running=False)
                active_ref, active_status, active_msg = find_active_backend(username)

                if active_status == "RUNNING":
                    active_slug = active_ref.split("/")[-1]
                    print(f"🎉 [TÁI SỬ DỤNG] Phát hiện phiên đang chạy trên {api_label}: {active_ref} ({active_msg})!")
                    report_kernel_state("running", f"Máy chủ GPU '{active_slug}' đang chạy ổn định. Đang kết nối...", active_msg, api_label)
                    ok = wait_for_live_backend_and_test(username, active_slug)
                    if ok:
                        sys.exit(0)

                if active_status == "QUEUED":
                    active_slug = active_ref.split("/")[-1]
                    print(f"⏳ [HÀNG ĐỢI SẴN CÓ] Notebook '{active_slug}' đang trong hàng đợi cấp GPU trên {api_label}!")
                    report_kernel_state("queued", f"Notebook '{active_slug}' đang trong hàng đợi cấp GPU...", active_msg, api_label)
                    got_running, _ = monitor_queue_and_retry(username, active_slug, acc.get("token"), timeout_seconds=105)
                    if got_running:
                        ok = wait_for_live_backend_and_test(username, active_slug)
                        if ok:
                            sys.exit(0)
                    if len(accounts) > 1:
                        print(f"🔄 [ROTATE] '{active_slug}' chờ quá 105s. Xóa để giải phóng slot và xoay sang API kế tiếp...")
                        subprocess.run([*KAGGLE_CMD, "kernels", "delete", "-y", active_ref], capture_output=True, text=True, timeout=10)
                        time.sleep(4)
                        continue

            new_slug = generate_unique_slug()
            print(f"✨ [NEW GPU NOTEBOOK] Khởi tạo '{new_slug}' trên {api_label}...")
            report_kernel_state("pushing", f"Đang khởi tạo '{new_slug}' trên GPU ({api_label})...", None, api_label)

            success = False
            push_msg = ""
            cleaned_running_slots = bool(FORCE_PUSH)

            for use_gpu, m_shape, label in gpu_accelerators:
                update_kernel_metadata(username, new_slug, use_gpu=use_gpu, machine_shape=m_shape)
                print(f"🚀 Đang đẩy '{new_slug}' lên Kaggle {label} ({api_label})...")
                success, push_msg = push_kernel()

                if not success:
                    lower_msg = push_msg.lower()
                    # Nếu hết 30h GPU/tuần -> đánh dấu ngay và xoay sang API tiếp theo!
                    if "maximum weekly gpu quota" in lower_msg or "weekly gpu quota" in lower_msg:
                        print(f"   ⚠️ {api_label} đã hết sạch 30h quota GPU/tuần! Xoay ngay sang API tiếp theo...")
                        gpu_quota_exhausted.add(username)
                        break

                    # Nếu đầy 2 slot GPU và chưa xóa các phiên synexan đang chạy -> Xóa ngay để lấy lại slot GPU rồi thử lại!
                    if ("maximum batch gpu session count" in lower_msg or "session count of 2 reached" in lower_msg) and not cleaned_running_slots:
                        print(f"   🧹 {api_label} báo đầy 2/2 slot GPU. Đang tự động dọn các phiên synexan cũ để lấy lại slot GPU...")
                        deleted_n = cleanup_stale_kernels(username, current_slug=new_slug, force_kill_running=True)
                        cleaned_running_slots = True
                        if deleted_n > 0:
                            print(f"   🔄 Đã giải phóng {deleted_n} phiên cũ! Đang đẩy lại {label} trên {api_label}...")
                            success, push_msg = push_kernel()

                if success:
                    print(f"✅ THÀNH CÔNG! Đã khởi tạo '{new_slug}' trên {label} ({api_label}).")
                    k_url = f"https://www.kaggle.com/code/{username}/{new_slug}"
                    report_kernel_state("queued", f"Đã khởi tạo '{new_slug}' trên {label}. Đang nhận GPU...", k_url, api_label)
                    break
                else:
                    lower_msg = push_msg.lower()
                    if "maximum batch gpu session count" in lower_msg or "session count of 2 reached" in lower_msg:
                        print(f"   ⚠️ {api_label} hết slot cho {label}, thử cấu hình GPU kế tiếp...")
                        time.sleep(2)
                        continue
                    else:
                        print(f"   ⚠️ Lỗi push {label}: {push_msg}")
                        break

            if success:
                got_running, _ = monitor_queue_and_retry(username, new_slug, acc.get("token"), timeout_seconds=105)
                if got_running:
                    ok = wait_for_live_backend_and_test(username, new_slug)
                    if ok:
                        sys.exit(0)
                print(f"🔄 [ROTATE] '{new_slug}' chưa nhận được GPU sau 105s. Xóa để giải phóng slot và kiểm tra API tiếp theo...")
                subprocess.run([*KAGGLE_CMD, "kernels", "delete", "-y", f"{username}/{new_slug}"], capture_output=True, text=True, timeout=10)
                time.sleep(4)
            else:
                print(f"🔄 Xoay sang mã API tiếp theo trong danh sách Secret...")

    # ==========================================================================
    # GIAI ĐOẠN 2 (DỰ PHÒNG CUỐI CÙNG): CHỈ CHẠY CPU KHI 100% CÁC API ĐỀU HẾT GPU
    # ==========================================================================
    print("\n" + "=" * 70)
    print("⚠️ [GIAI ĐOẠN 2 - CPU FALLBACK] Toàn bộ các mã API trong danh sách đều đã hết GPU!")
    print("👉 Kích hoạt chế độ chạy CPU Đa Luồng dự phòng để duy trì hệ thống trực tuyến...")
    print("=" * 70)

    for idx, acc in enumerate(accounts, start=1):
        username = acc["username"]
        key = acc["key"]
        api_label = f"API #{idx}/{len(accounts)}"
        setup_kaggle_credentials(username, key, acc.get("token"))
        cleanup_stale_kernels(username, force_kill_running=True)

        new_slug = generate_unique_slug()
        label = "CPU Đa Luồng (Dự phòng khi toàn bộ API hết GPU)"
        update_kernel_metadata(username, new_slug, use_gpu=False, machine_shape=None)
        print(f"🚀 Đang đẩy '{new_slug}' lên Kaggle {label} ({api_label})...")
        report_kernel_state("pushing", f"Toàn bộ API đã hết GPU. Đang bật {label}...", None, api_label)
        success, push_msg = push_kernel()
        if success:
            got_running, _ = monitor_queue_and_retry(username, new_slug, acc.get("token"), timeout_seconds=90)
            if got_running:
                ok = wait_for_live_backend_and_test(username, new_slug)
                if ok:
                    sys.exit(0)

    print("=" * 70)
    print("🏁 [SUPERVISOR KẾT THÚC] Các phiên đang tiếp tục chạy ngầm trên Kaggle.")
    print("=" * 70)


if __name__ == "__main__":
    main()
