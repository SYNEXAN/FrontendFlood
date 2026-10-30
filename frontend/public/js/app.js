/**
 * AI Urban Flood & Traffic Monitor - Main Frontend Application Logic
 */

let currentTab = "tab_fusion";
let audioContext = null;
let isAudioEnabled = true;

document.addEventListener("DOMContentLoaded", () => {
    initTabs();
    initSettingsModal();
    initUploads();
    initStreamPlayer();
    checkBackendHealth();
});

// 1. Initial Health Check
async function checkBackendHealth() {
    window.updateStatusBadge("checking");
    const backendUrl = window.CONFIG.getBackendUrl();
    try {
        const res = await fetch(`${backendUrl}/api/v1/health`);
        if (res.ok) {
            const data = await res.json();
            window.updateStatusBadge("online", data.gpu_name || "Tesla T4");
        } else {
            window.updateStatusBadge("offline");
        }
    } catch (_) {
        window.updateStatusBadge("offline");
    }
}

// 2. Tab Navigation
function initTabs() {
    const tabs = ["tab_flood", "tab_traffic", "tab_fusion"];
    tabs.forEach(t => {
        const btn = document.getElementById(`btn-${t}`);
        if (btn) {
            btn.addEventListener("click", () => switchTab(t));
        }
    });
}

function switchTab(targetTab) {
    currentTab = targetTab;
    const tabs = ["tab_flood", "tab_traffic", "tab_fusion"];
    tabs.forEach(t => {
        const btn = document.getElementById(`btn-${t}`);
        const pane = document.getElementById(`pane-${t}`);
        if (t === targetTab) {
            btn.className = "flex items-center gap-2 px-5 py-2.5 rounded-xl font-medium text-sm transition-all duration-200 bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 shadow-lg shadow-cyan-500/10";
            pane.classList.remove("hidden");
        } else {
            btn.className = "flex items-center gap-2 px-5 py-2.5 rounded-xl font-medium text-sm transition-all duration-200 text-slate-400 hover:text-slate-200 hover:bg-slate-800/50";
            pane.classList.add("hidden");
        }
    });
}

// 3. Image Upload & Analysis Handler
function initUploads() {
    setupUploadPane("flood", "/api/v1/predict/image");
    setupUploadPane("traffic", "/api/v1/predict/image");
    setupUploadPane("fusion", "/api/v1/predict/image");
}

function setupUploadPane(prefix, endpoint) {
    const fileInput = document.getElementById(`${prefix}-file-input`);
    const dropzone = document.getElementById(`${prefix}-dropzone`);
    const previewImg = document.getElementById(`${prefix}-preview-img`);
    const outputImg = document.getElementById(`${prefix}-output-img`);
    const runBtn = document.getElementById(`${prefix}-run-btn`);
    const spinner = document.getElementById(`${prefix}-spinner`);

    let selectedFile = null;

    if (fileInput) {
        fileInput.addEventListener("change", (e) => {
            if (e.target.files && e.target.files[0]) {
                handleFile(e.target.files[0]);
            }
        });
    }

    if (dropzone) {
        dropzone.addEventListener("dragover", (e) => {
            e.preventDefault();
            dropzone.classList.add("border-cyan-500", "bg-cyan-500/5");
        });
        dropzone.addEventListener("dragleave", () => {
            dropzone.classList.remove("border-cyan-500", "bg-cyan-500/5");
        });
        dropzone.addEventListener("drop", (e) => {
            e.preventDefault();
            dropzone.classList.remove("border-cyan-500", "bg-cyan-500/5");
            if (e.dataTransfer.files && e.dataTransfer.files[0]) {
                handleFile(e.dataTransfer.files[0]);
            }
        });
    }

    function handleFile(file) {
        selectedFile = file;
        const reader = new FileReader();
        reader.onload = (e) => {
            previewImg.src = e.target.result;
            previewImg.classList.remove("hidden");
            if (dropzone) dropzone.classList.add("hidden");
            if (runBtn) runBtn.disabled = false;
        };
        reader.readAsDataURL(file);
    }

    if (runBtn) {
        runBtn.addEventListener("click", async () => {
            if (!selectedFile) return;

            runBtn.disabled = true;
            if (spinner) spinner.classList.remove("hidden");

            const formData = new FormData();
            formData.append("file", selectedFile);
            formData.append("gauge_type", "staff_gauge");
            formData.append("conf", "0.35");

            try {
                const data = await window.smartFetch(endpoint, {
                    method: "POST",
                    body: formData
                });

                if (data && data.status === "success") {
                    renderAnalysisResults(prefix, data);
                } else {
                    alert("Không nhận được kết quả dự đoán. Vui lòng thử lại!");
                }
            } catch (err) {
                console.error("Predict error:", err);
            } finally {
                runBtn.disabled = false;
                if (spinner) spinner.classList.add("hidden");
            }
        });
    }
}

// 4. Render Results to KPI cards and Images
function renderAnalysisResults(prefix, data) {
    const outputImg = document.getElementById(`${prefix}-output-img`);
    const placeholder = document.getElementById(`${prefix}-output-placeholder`);

    if (outputImg && data.annotated_image) {
        outputImg.src = data.annotated_image;
        outputImg.classList.remove("hidden");
        if (placeholder) placeholder.classList.add("hidden");
    }

    // Update KPI Badges
    const depthEl = document.getElementById(`${prefix}-kpi-depth`);
    if (depthEl) {
        depthEl.innerText = `${data.flood.depth_cm} cm`;
        const sev = data.flood.severity;
        depthEl.className = `text-2xl font-bold ${sev === 'DANGER' ? 'text-rose-400' : (sev === 'WARNING' ? 'text-amber-400' : 'text-emerald-400')}`;
    }

    const countEl = document.getElementById(`${prefix}-kpi-vehicles`);
    if (countEl) countEl.innerText = `${data.traffic.total_vehicles} xe`;

    const pcuEl = document.getElementById(`${prefix}-kpi-pcu`);
    if (pcuEl) pcuEl.innerText = `${data.traffic.pcu_total} PCU`;

    const congEl = document.getElementById(`${prefix}-kpi-congestion`);
    if (congEl) congEl.innerText = data.traffic.congestion_level;

    // Dual Alarm Banner Check
    const alarmBanner = document.getElementById("dual-alarm-banner");
    const alarmMsg = document.getElementById("dual-alarm-msg");
    if (alarmBanner && alarmMsg) {
        if (data.dual_alarm && data.dual_alarm.triggered) {
            alarmBanner.classList.remove("hidden");
            alarmMsg.innerText = data.dual_alarm.message;
            triggerAlarmSound();
        } else {
            alarmBanner.classList.add("hidden");
        }
    }
}

// 5. Real-Time CCTV Streaming Player
function initStreamPlayer() {
    const startStreamBtn = document.getElementById("start-stream-btn");
    const stopStreamBtn = document.getElementById("stop-stream-btn");
    const streamImg = document.getElementById("stream-video-element");
    const streamSourceInput = document.getElementById("stream-source-url");
    const streamStatus = document.getElementById("stream-status-indicator");

    if (startStreamBtn && streamImg) {
        startStreamBtn.addEventListener("click", () => {
            const source = (streamSourceInput && streamSourceInput.value.trim()) 
                ? streamSourceInput.value.trim() 
                : "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8";

            const backendUrl = window.CONFIG.getBackendUrl();
            const liveUrl = `${backendUrl}/api/v1/stream/live?source=${encodeURIComponent(source)}`;
            
            streamImg.src = liveUrl;
            streamImg.classList.remove("hidden");
            if (streamStatus) streamStatus.innerText = "🔴 Đang truyền trực tiếp (30 FPS GPU)";
            startStreamBtn.classList.add("hidden");
            if (stopStreamBtn) stopStreamBtn.classList.remove("hidden");
        });
    }

    if (stopStreamBtn && streamImg) {
        stopStreamBtn.addEventListener("click", () => {
            streamImg.src = "";
            streamImg.classList.add("hidden");
            if (streamStatus) streamStatus.innerText = "⚪ Sẵn sàng kết nối";
            stopStreamBtn.classList.add("hidden");
            if (startStreamBtn) startStreamBtn.classList.remove("hidden");
        });
    }
}

// 6. Web Audio Siren for Dual Alarm
function triggerAlarmSound() {
    if (!isAudioEnabled) return;
    try {
        if (!audioContext) {
            audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }
        if (audioContext.state === "suspended") {
            audioContext.resume();
        }

        const osc = audioContext.createOscillator();
        const gain = audioContext.createGain();
        osc.connect(gain);
        gain.connect(audioContext.destination);

        osc.type = "sawtooth";
        osc.frequency.setValueAtTime(600, audioContext.currentTime);
        osc.frequency.exponentialRampToValueAtTime(900, audioContext.currentTime + 0.3);
        osc.frequency.exponentialRampToValueAtTime(600, audioContext.currentTime + 0.6);

        gain.gain.setValueAtTime(0.15, audioContext.currentTime);
        gain.gain.linearRampToValueAtTime(0, audioContext.currentTime + 0.6);

        osc.start();
        osc.stop(audioContext.currentTime + 0.6);
    } catch (e) {
        console.log("Audio alert suppressed:", e);
    }
}

// 7. Settings Modal for Dynamic Backend URL
function initSettingsModal() {
    const openBtn = document.getElementById("open-settings-btn");
    const closeBtn = document.getElementById("close-settings-btn");
    const modal = document.getElementById("settings-modal");
    const input = document.getElementById("custom-backend-input");
    const saveBtn = document.getElementById("save-settings-btn");
    const resetBtn = document.getElementById("reset-settings-btn");

    if (openBtn && modal) {
        openBtn.addEventListener("click", () => {
            if (input) input.value = window.CONFIG.getBackendUrl();
            modal.classList.remove("hidden");
        });
    }

    if (closeBtn && modal) {
        closeBtn.addEventListener("click", () => modal.classList.add("hidden"));
    }

    if (saveBtn) {
        saveBtn.addEventListener("click", () => {
            if (input && input.value) {
                window.CONFIG.setBackendUrl(input.value);
                alert("Đã lưu địa chỉ Backend mới! Đang kiểm tra kết nối...");
                modal.classList.add("hidden");
                checkBackendHealth();
            }
        });
    }

    if (resetBtn) {
        resetBtn.addEventListener("click", () => {
            window.CONFIG.resetToDefault();
            if (input) input.value = window.CONFIG.getBackendUrl();
            alert("Đã khôi phục địa chỉ mặc định!");
            modal.classList.add("hidden");
            checkBackendHealth();
        });
    }
}
