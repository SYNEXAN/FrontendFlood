/**
 * Transparent Auto-Wake Interceptor & API Client
 */
let isAutoWaking = false;
let wakeInterval = null;

async function smartFetch(endpoint, options = {}) {
    const backendUrl = window.CONFIG.getBackendUrl();
    const targetUrl = `${backendUrl}${endpoint}`;

    try {
        const response = await fetch(targetUrl, options);
        if (!response.ok && response.status >= 500) {
            throw new Error(`Server returned ${response.status}`);
        }
        return await response.json();
    } catch (err) {
        console.warn(`[WARN] Backend connection failed to ${targetUrl}. Activating transparent Auto-Wake...`, err);
        return await handleAutoWake(endpoint, options);
    }
}

async function handleAutoWake(endpoint, options) {
    if (isAutoWaking) {
        console.log("[INFO] Auto-wake already in progress. Please wait...");
        return;
    }
    isAutoWaking = true;
    showWakeModal();

    // 1. Dispatch wake request to Render server
    try {
        await fetch('/api/wake-up', { method: 'POST' });
        console.log("[INFO] Wake-up signal dispatched.");
    } catch (e) {
        console.log("[INFO] Auto-wake dispatch sent to local endpoint.");
    }

    // 2. Poll Backend health
    let elapsed = 0;
    const maxWaitSeconds = 150;
    const backendUrl = window.CONFIG.getBackendUrl();

    return new Promise((resolve, reject) => {
        wakeInterval = setInterval(async () => {
            elapsed += 5;
            updateWakeProgress(elapsed, maxWaitSeconds);

            try {
                const healthRes = await fetch(`${backendUrl}/api/v1/health`, { method: "GET" });
                if (healthRes.ok) {
                    const data = await healthRes.json();
                    if (data.status === "ready") {
                        clearInterval(wakeInterval);
                        isAutoWaking = false;
                        hideWakeModal();
                        updateStatusBadge("online", data.gpu_name || "Tesla T4");

                        // 3. Transparently retry the original request
                        console.log("[SUCCESS] AI Backend is READY! Retrying original request...");
                        try {
                            const retryRes = await fetch(`${backendUrl}${endpoint}`, options);
                            const result = await retryRes.json();
                            resolve(result);
                        } catch (retryErr) {
                            reject(retryErr);
                        }
                    }
                }
            } catch (_) {
                // Still waiting for GPU container
            }

            if (elapsed >= maxWaitSeconds) {
                clearInterval(wakeInterval);
                isAutoWaking = false;
                hideWakeModal();
                alert("Máy chủ Kaggle GPU khởi động quá thời gian chờ (150s). Vui lòng kiểm tra lại notebook hoặc bấm thử lại!");
                reject(new Error("Auto-wake timed out"));
            }
        }, 5000);
    });
}

function showWakeModal() {
    const modal = document.getElementById("auto-wake-modal");
    if (modal) modal.classList.remove("hidden");
    const bar = document.getElementById("wake-progress-bar");
    if (bar) bar.style.width = "5%";
    const txt = document.getElementById("wake-status-text");
    if (txt) txt.innerText = "Đang gửi lệnh cấp phát GPU Tesla T4 trên Kaggle...";
}

function updateWakeProgress(elapsed, total) {
    const percent = Math.min(95, Math.round((elapsed / total) * 100));
    const bar = document.getElementById("wake-progress-bar");
    if (bar) bar.style.width = `${percent}%`;

    const txt = document.getElementById("wake-status-text");
    if (txt) {
        if (elapsed < 30) {
            txt.innerText = `Đang cấp phát máy chủ GPU Tesla T4... (${elapsed}s)`;
        } else if (elapsed < 60) {
            txt.innerText = `Đang boot Linux & cài đặt thư viện AI... (${elapsed}s)`;
        } else if (elapsed < 90) {
            txt.innerText = `Đang nạp 2 mô hình YOLOv8s vào VRAM... (${elapsed}s)`;
        } else {
            txt.innerText = `Đang thiết lập cổng kết nối bảo mật Ngrok... (${elapsed}s)`;
        }
    }
}

function hideWakeModal() {
    const modal = document.getElementById("auto-wake-modal");
    if (modal) modal.classList.add("hidden");
}

function updateStatusBadge(status, details = "") {
    const badge = document.getElementById("backend-status-badge");
    const dot = document.getElementById("status-dot");
    const text = document.getElementById("status-text");
    if (!badge || !dot || !text) return;

    if (status === "online") {
        badge.className = "inline-flex items-center gap-2 px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30";
        dot.className = "w-2 h-2 rounded-full bg-emerald-500 animate-pulse";
        text.innerText = `AI Backend: Online (${details || 'GPU Ready'})`;
    } else if (status === "checking") {
        badge.className = "inline-flex items-center gap-2 px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/30";
        dot.className = "w-2 h-2 rounded-full bg-amber-500 animate-ping";
        text.innerText = "AI Backend: Đang kiểm tra...";
    } else {
        badge.className = "inline-flex items-center gap-2 px-3 py-1 rounded-full text-xs font-semibold bg-rose-500/10 text-rose-400 border border-rose-500/30";
        dot.className = "w-2 h-2 rounded-full bg-rose-500";
        text.innerText = "AI Backend: Ngoại tuyến (Sẽ tự bật khi dùng)";
    }
}

window.smartFetch = smartFetch;
window.updateStatusBadge = updateStatusBadge;
