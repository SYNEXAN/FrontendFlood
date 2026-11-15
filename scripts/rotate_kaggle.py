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
FORCE_PUSH = os.environ.get("FORCE_PUSH", "false").lower() in ("true", "1", "yes")
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
    """Tự động nhận diện username từ Kaggle API khi người dùng chỉ cung cấp Token (KGAT_...)."""
    KNOWN_USERNAMES = ["nguynthanhthit", "cqqdoday", "jtachitv"]
    try:
        url = "https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernels"
        req = urllib.request.Request(
            url,
            data=json.dumps({"group": "PROFILE", "pageSize": 5}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data and isinstance(data.get("kernels"), list) and len(data["kernels"]) > 0:
                ref = data["kernels"][0].get("ref", "")
                if "/" in ref:
                    return ref.split("/")[0]
    except Exception as e:
        print(f"   [Auto-Username] Không thể tự nhận diện username từ Token: {e}")
    if fallback_index is not None and fallback_index < len(KNOWN_USERNAMES):
        fb_user = KNOWN_USERNAMES[fallback_index]
        print(f"   [Auto-Username Fallback] Sử dụng username định danh từ danh sách tài khoản: {fb_user}")
        return fb_user
    return None


def load_accounts():
    """Tải danh sách tài khoản từ KAGGLE_ACCOUNTS_JSON (hỗ trợ file json, token KGAT, hoặc key đơn lẻ)."""
    raw_json = os.environ.get("KAGGLE_ACCOUNTS_JSON", "").strip()
    accounts = []

    if raw_json:
        # 1. Thử parse JSON chuẩn
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                accounts = parsed
            elif isinstance(parsed, dict):
                accounts = [parsed]
            elif isinstance(parsed, str):
                accounts = [{"token": parsed}]
        except Exception:
            pass

        # 2. Thử parse bằng ast.literal_eval
        if not accounts:
            try:
                parsed = ast.literal_eval(raw_json)
                if isinstance(parsed, list):
                    accounts = parsed
                elif isinstance(parsed, dict):
                    accounts = [parsed]
            except Exception:
                pass

        # 3. Thử trích xuất Token KGAT_... đơn lẻ hoặc danh sách
        if not accounts:
            kgat_tokens = re.findall(r'KGAT_[a-zA-Z0-9_\-]+', raw_json)
            if kgat_tokens:
                accounts = [{"token": tok} for tok in kgat_tokens]

        # 4. Thử trích xuất {username: xxx, key: yyy} bằng Regex
        if not accounts:
            matches = re.findall(
                r'username["\']?\s*:\s*["\']?([^,"\';\s}]+)["\']?.*?key["\']?\s*:\s*["\']?([^,"\';\s}]+)["\']?',
                raw_json,
                re.DOTALL
            )
            if matches:
                accounts = [{"username": u.strip(), "key": k.strip()} for u, k in matches]

    if not accounts:
        user = os.environ.get("KAGGLE_USERNAME", "").strip()
        key = os.environ.get("KAGGLE_KEY", "").strip()
        if user and key:
            accounts = [{"username": user, "key": key}]

    # Xử lý tự động phát hiện Username nếu người dùng chỉ cung cấp Key / Token
    resolved = []
    for idx, item in enumerate(accounts):
        if isinstance(item, str):
            item = {"token": item} if item.startswith("KGAT_") else {"key": item}

        u = item.get("username", "").strip()
        k = item.get("key", "").strip()
        tok = item.get("token", "").strip() or (k if k.startswith("KGAT_") else "")

        if not u and tok:
            print(f"ℹ️ Phát hiện Token không có Username. Đang tự động nhận diện username từ Kaggle API...")
            discovered_user = resolve_username_from_token(tok, fallback_index=idx)
            if discovered_user:
                print(f"✅ Đã tự động nhận diện Username: {discovered_user}")
                u = discovered_user

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
    if key:
        data["key"] = key
        os.environ["KAGGLE_KEY"] = key

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
            [*KAGGLE_CMD, "kernels", "list", "--mine", "-s", "synexan", "-v"],
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
                        return kernel_ref, "RUNNING", st_out
                    elif "kernelworkerstatus.queued" in lower_st or '"queued"' in lower_st:
                        # Kiểm tra xem phiên QUEUED này có phải zombie queue (cũ > 3 phút)
                        age = get_slug_age_seconds(kernel_ref)
                        if age is not None and age > 180:
                            print(f"   🛑 Phiên QUEUED '{kernel_ref}' đã kẹt {int(age // 60)} phút trước (zombie queue). Đang hủy để giải phóng GPU slot...")
                            subprocess.run([*KAGGLE_CMD, "kernels", "cancel", kernel_ref], capture_output=True, text=True, timeout=10)
                            time.sleep(4)
                            subprocess.run([*KAGGLE_CMD, "kernels", "delete", "-y", kernel_ref], capture_output=True, text=True, timeout=10)
                            continue
                        elif not first_queued and not kernel_ref.lower().endswith("synexan-flood-backend-gpu"):
                            first_queued = (kernel_ref, "QUEUED", st_out)

        if first_queued:
            return first_queued

    except Exception as e:
        print(f"   [find_active_backend] Lỗi: {e}")

    return None, "NONE", None


def cleanup_stale_kernels(username, current_slug=None):
    """
    Tự động dọn dẹp các notebook synexan-* cũ của username.
    Sử dụng '-s synexan -v' để phát hiện chính xác tất cả phiên synexan.
    Hủy các phiên QUEUED quá hạn > 3 phút để giải phóng slot GPU.
    """
    print(f"🧹 [Auto-Cleanup] Quét và dọn dẹp các notebook synexan cũ của '{username}' (-s synexan -v)...")
    try:
        proc = subprocess.run(
            [*KAGGLE_CMD, "kernels", "list", "--mine", "-s", "synexan", "-v"],
            capture_output=True,
            text=True,
            timeout=20
        )
        lines = proc.stdout.strip().splitlines()
        if len(lines) > 1:
            reader = csv.DictReader(lines)
            for row in reader:
                kernel_ref = row.get("ref", "").strip()
                if not kernel_ref:
                    continue
                if username.lower() in kernel_ref.lower() and "synexan" in kernel_ref.lower():
                    if current_slug and current_slug.lower() in kernel_ref.lower():
                        continue
                    # Bảo vệ tuyệt đối notebook Public cố định để link trên Web không bao giờ bị 404
                    if kernel_ref.lower().endswith("/synexan-flood-backend-gpu"):
                        continue

                    time.sleep(1) # Nghỉ nhẹ giữa các lệnh để tôn trọng rate limit
                    st_proc = subprocess.run(
                        [*KAGGLE_CMD, "kernels", "status", kernel_ref],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    st_out = (st_proc.stdout + st_proc.stderr).lower()
                    
                    age = get_slug_age_seconds(kernel_ref)
                    is_zombie_queue = ("queued" in st_out) and (age is not None and age > 180)

                    # 1. Hủy bất kỳ phiên synexan cũ nào đang kẹt QUEUED > 3 phút hoặc cũ để giải phóng slot GPU
                    if is_zombie_queue or (FORCE_PUSH and "running" in st_out):
                        print(f"   🛑 Đang hủy phiên synexan kẹt ({'zombie queue' if is_zombie_queue else 'force push'}): {kernel_ref}")
                        subprocess.run([*KAGGLE_CMD, "kernels", "cancel", kernel_ref], capture_output=True, text=True, timeout=10)
                        time.sleep(6) # Đợi Kaggle giải phóng slot trong cụm máy chủ
                        st_out = "cancel_acknowledged"

                    # 2. Xóa các kernel synexan đã kết thúc/lỗi/hủy để danh sách luôn sạch
                    if "complete" in st_out or "error" in st_out or "cancel_acknowledged" in st_out:
                        print(f"   🗑️ Đang xóa kernel synexan đã kết thúc: {kernel_ref}")
                        subprocess.run([*KAGGLE_CMD, "kernels", "delete", "-y", kernel_ref], capture_output=True, text=True, timeout=10)
                        time.sleep(1)
    except Exception as e:
        print(f"   [Auto-Cleanup] Lỗi quét kernel: {e}")


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

    # 0. Kiểm tra trước xem đã có Backend GPU nào đang Online trên Vercel hay chưa (tránh đè phiên chạy thủ công)
    if not FORCE_PUSH:
        try:
            req_pre = urllib.request.Request(
                "https://frontend-flood.vercel.app/api/config",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req_pre, timeout=8) as resp:
                pre_data = json.loads(resp.read().decode("utf-8"))
                existing_url = (pre_data.get("defaultBackendUrl") or "").strip()
                if existing_url and existing_url.startswith("http") and "kaggle.com" not in existing_url:
                    is_alive, _, probe_msg = verify_backend_health(existing_url, timeout=6)
                    if is_alive:
                        print(f"🎉 [ALREADY ONLINE] Phát hiện Backend GPU đang hoạt động tại: {existing_url} ({probe_msg})")
                        print("👉 Giữ nguyên phiên hiện tại, không đẩy thêm notebook mới.")
                        report_kernel_state("ready", "Backend GPU đang hoạt động ổn định!", existing_url, "active")
                        sys.exit(0)
        except Exception as e:
            print(f"ℹ️ [Pre-check] Chưa xác nhận được backend online sẵn: {e}")

    accounts = load_accounts()
    if not accounts:
        print("❌ LỖI: Không tìm thấy bất kỳ tài khoản Kaggle nào trong KAGGLE_ACCOUNTS_JSON!")
        print("Vui lòng cấu hình Secret KAGGLE_ACCOUNTS_JSON trong Settings -> Secrets của GitHub.")
        sys.exit(1)

    print(f"📋 Tìm thấy {len(accounts)} tài khoản trong danh sách dự phòng.")
    print(f"🎯 Ngưỡng an toàn tối thiểu: {MIN_GPU_HOURS_THRESHOLD}h GPU/tuần.")

    MAX_SUPERVISOR_ROUNDS = 4
    for round_idx in range(1, MAX_SUPERVISOR_ROUNDS + 1):
        print(f"\n" + "=" * 70)
        print(f"🔄 [VÒNG GIÁM SÁT {round_idx}/{MAX_SUPERVISOR_ROUNDS}] Kiểm tra toàn diện danh sách tài khoản...")
        print("=" * 70)

        for idx, acc in enumerate(accounts, start=1):
            username = acc["username"]
            key = acc["key"]
            masked_key = key[:4] + "****" if len(key) > 4 else "****"
            print(f"\n🔍 [Tài khoản {idx}/{len(accounts)}] Đang kiểm tra: {username} (Key: {masked_key})...")

            # 1. Cấu hình credentials
            setup_kaggle_credentials(username, key, acc.get("token"))

            # 1b. Tự động dọn dẹp các phiên synexan cũ đã kết thúc/lỗi hoặc kẹt QUEUED > 3 phút
            cleanup_stale_kernels(username)

            # 2. Kiểm tra xem có notebook backend nào của username ĐANG CHẠY HOẶC ĐANG CHỜ HÀNG ĐỢI
            if not FORCE_PUSH:
                active_ref, active_status, active_msg = find_active_backend(username)

                if active_status == "RUNNING":
                    active_slug = active_ref.split("/")[-1]
                    print(f"🎉 [TÁI SỬ DỤNG] Phát hiện máy chủ GPU đang chạy: {active_ref} ({active_msg})!")
                    try:
                        log_p = subprocess.run([*KAGGLE_CMD, "kernels", "logs", active_ref], capture_output=True, text=True, timeout=10)
                        print(f"📄 [Kernel Logs {active_ref}]:\n{log_p.stdout[-1500:] if log_p.stdout else '(Trống)'}")
                    except Exception as le:
                        print(f"   [Logs error]: {le}")
                    print("👉 Tiến hành giám sát kết nối và kiểm thử suy luận...")
                    report_kernel_state("running", f"Máy chủ GPU '{active_ref}' đang chạy ổn định. Đang kết nối trực tiếp...", active_msg, username)
                    ok = wait_for_live_backend_and_test(username, active_slug)
                    if ok:
                        sys.exit(0)

                if active_status == "QUEUED":
                    active_slug = active_ref.split("/")[-1]
                    print(f"⏳ [PHÁT HIỆN HÀNG ĐỢI SẴN CÓ] Notebook '{active_ref}' đã ở trong hàng đợi Kaggle cấp GPU!")
                    print("👉 Tuyệt đối không đẩy thêm notebook mới để tránh lỗi quá tải 2 GPU sessions.")
                    report_kernel_state("queued", f"Notebook '{active_slug}' đang trong hàng đợi Kaggle cấp GPU. Đang theo dõi...", active_msg, username)

                    got_running, _ = monitor_queue_and_retry(username, active_slug, acc.get("token"), timeout_seconds=105)
                    if got_running:
                        ok = wait_for_live_backend_and_test(username, active_slug)
                        if ok:
                            sys.exit(0)

                    # Nếu sau 105s vẫn QUEUED:
                    if len(accounts) > 1 and idx < len(accounts):
                        print(f"🔄 [ROTATE] Notebook '{active_ref}' vẫn đang QUEUED sau 105s. Hủy phiên kẹt để giải phóng slot và kiểm tra tài khoản tiếp theo...")
                        subprocess.run([*KAGGLE_CMD, "kernels", "cancel", active_ref], capture_output=True, text=True, timeout=10)
                        time.sleep(6)
                        continue
                    else:
                        print(f"⏳ Notebook '{active_ref}' tiếp tục xếp hàng trên Kaggle. Frontend sẽ tự kết nối khi GPU khởi động.")
                        report_kernel_state("queued", f"Notebook '{active_slug}' đang trong hàng đợi cấp GPU của Kaggle. Vui lòng chờ trong giây lát, backend sẽ tự động sẵn sàng.", None, username)
                        continue
            else:
                print(f"⚡ [FORCE_PUSH] Bắt buộc đẩy Notebook mới, dọn dẹp các phiên cũ của '{username}'...")

            # 3. Không có notebook nào đang chạy hoặc chờ -> Khởi tạo Notebook Mới Toanh
            new_slug = generate_unique_slug()
            print(f"✨ [NEW NOTEBOOK] Chuẩn bị khởi tạo Notebook mới toanh: '{username}/{new_slug}'")
            report_kernel_state("pushing", f"Đang chuẩn bị khởi tạo Notebook mới toanh: '{new_slug}'...", None, username)

            # 4. Tự động dọn dẹp các notebook cũ đã kết thúc/lỗi để tránh rác
            cleanup_stale_kernels(username, current_slug=new_slug)
            time.sleep(2)

            # 5 & 6. Đẩy Notebook Mới lên Kaggle với cơ chế đa tầng (Dual T4 -> Single GPU -> CPU Fallback)
            accelerator_options = [
                (True, "NvidiaTeslaT4", "GPU Tesla T4 (Dual T4)"),
                (True, None, "GPU Tesla P100 (Single GPU)"),
            ]
            if round_idx >= 3:
                accelerator_options.append((False, None, "CPU Đa Luồng (Instant Fallback)"))

            success = False
            push_msg = ""
            for use_gpu, m_shape, label in accelerator_options:
                update_kernel_metadata(username, new_slug, use_gpu=use_gpu, machine_shape=m_shape)
                print(f"🚀 Đang đẩy Notebook mới toanh '{new_slug}' lên Kaggle {label}...")
                report_kernel_state("pushing", f"Đang đẩy Notebook mới toanh '{new_slug}' lên Kaggle {label}...", None, username)
                success, push_msg = push_kernel()
                if success:
                    print(f"✅ THÀNH CÔNG! Đã khởi tạo Notebook mới '{new_slug}' trên {label} cho '{username}'.")
                    k_url = f"https://www.kaggle.com/code/{username}/{new_slug}"
                    report_kernel_state("queued", f"Đã khởi tạo Notebook mới '{new_slug}'. Đang vào hàng đợi cấp {label}...", k_url, username)
                    break
                else:
                    lower_msg = push_msg.lower()
                    if "maximum batch gpu session count" in lower_msg or "session count of 2 reached" in lower_msg:
                        print(f"   ⚠️ Tài khoản '{username}' hết slot cho {label}, thử cấu hình accelerator kế tiếp...")
                        time.sleep(2)
                        continue
                    else:
                        print(f"   ⚠️ Lỗi push {label}: {push_msg}")
                        break

            if success:
                # Giám sát 105s
                got_running, _ = monitor_queue_and_retry(username, new_slug, acc.get("token"), timeout_seconds=105)
                if got_running:
                    ok = wait_for_live_backend_and_test(username, new_slug)
                    if ok:
                        sys.exit(0)

                # Nếu sau 105s vẫn QUEUED, xoay sang tài khoản tiếp theo nếu có
                if len(accounts) > 1 and idx < len(accounts):
                    print(f"🔄 [ROTATE] Notebook '{new_slug}' chưa kịp nhận GPU sau 105s. Hủy để giải phóng slot và xoay sang tài khoản tiếp theo...")
                    subprocess.run([*KAGGLE_CMD, "kernels", "cancel", f"{username}/{new_slug}"], capture_output=True, text=True, timeout=10)
                    time.sleep(6)
                    continue
                else:
                    report_kernel_state("queued", f"Notebook '{new_slug}' đang xếp hàng nhận GPU. Frontend sẽ tự kết nối khi GPU sẵn sàng.", k_url, username)
                    continue
            else:
                lower_msg = push_msg.lower()
                if "maximum batch gpu session count" in lower_msg or "session count of 2 reached" in lower_msg:
                    print(f"⚠️ [CONCURRENT LIMIT] Tài khoản '{username}' bị đầy slot GPU bởi các notebook khác!")
                    report_kernel_state("limit", f"Tài khoản '{username}' đầy 2 slot GPU do các notebook khác. Đang tự động dọn dẹp và xoay vòng...", push_msg, username)
                    if len(accounts) > 1 and idx < len(accounts):
                        print("🔄 Xoay sang tài khoản tiếp theo...")
                        continue
                    else:
                        print("⏳ Đã duyệt hết các tài khoản, sẽ kiểm tra lại ở vòng giám sát kế tiếp...")

        if round_idx < MAX_SUPERVISOR_ROUNDS:
            print(f"\n⏳ [HẾT VÒNG {round_idx}] Tạm nghỉ 15s trước khi quét vòng kế tiếp...")
            time.sleep(15)

    print("=" * 70)
    print("🏁 [SUPERVISOR KẾT THÚC] Các phiên đang tiếp tục chạy ngầm trên Kaggle.")
    print("=" * 70)


if __name__ == "__main__":
    main()
