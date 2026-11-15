const express = require('express');
const path = require('path');
const cors = require('cors');
const fs = require('fs');

const app = express();
const PORT = process.env.PORT || 10000;
const DEFAULT_BACKEND_URL = process.env.DEFAULT_BACKEND_URL || "";
let activeBackendUrl = DEFAULT_BACKEND_URL;
let localKernelLog = null;

// ============================================================================
// GITHUB CREDENTIALS HELPER
// ============================================================================
function getGitHubCredentials() {
    let ghToken = process.env.GH_PAT || process.env.GITHUB_TOKEN;
    let ghRepo = process.env.GITHUB_REPOSITORY || 'SYNEXAN/FrontendFlood';
    if (!ghToken) {
        const envPaths = [path.join(__dirname, '..', '.env'), path.join(__dirname, '.env')];
        for (const ep of envPaths) {
            if (fs.existsSync(ep)) {
                try {
                    const envContent = fs.readFileSync(ep, 'utf8');
                    const matchPat = envContent.match(/GH_PAT\s*=\s*([^\r\n]+)/);
                    if (matchPat) ghToken = matchPat[1].trim();
                    const matchRepo = envContent.match(/GITHUB_REPOSITORY\s*=\s*([^\r\n]+)/);
                    if (matchRepo) ghRepo = matchRepo[1].trim();
                    if (ghToken) break;
                } catch (e) {}
            }
        }
    }
    return { ghToken, ghRepo };
}

// ============================================================================
// UPSTASH REDIS REST API & SERVERLESS STATE CLIENT
// ============================================================================
const TMP_STATE_FILE = '/tmp/synexan_backend_state.json';

function getUpstashConfig() {
    const redisUrl = (process.env.UPSTASH_REDIS_REST_URL || '')
        .trim()
        .replace(/^["']|["']$/g, '')
        .replace(/\/+$/, '');
    const redisToken = (process.env.UPSTASH_REDIS_REST_TOKEN || '')
        .trim()
        .replace(/^["']|["']$/g, '');
    return { redisUrl, redisToken };
}

function readTmpBackendUrl() {
    try {
        if (fs.existsSync(TMP_STATE_FILE)) {
            const raw = JSON.parse(fs.readFileSync(TMP_STATE_FILE, 'utf8'));
            if (raw && raw.url && typeof raw.url === 'string' && raw.url.startsWith('http')) {
                return raw.url;
            }
        }
    } catch (e) {}
    return '';
}

function writeTmpBackendUrl(url) {
    try {
        fs.writeFileSync(TMP_STATE_FILE, JSON.stringify({ url: url || '', updatedAt: Date.now() }), 'utf8');
    } catch (e) {}
}

async function getStoredBackendUrl() {
    const { redisUrl, redisToken } = getUpstashConfig();
    if (redisUrl && redisToken) {
        try {
            const res = await fetch(`${redisUrl}/get/activeBackendUrl`, {
                method: 'GET',
                headers: {
                    Authorization: `Bearer ${redisToken}`,
                    'Cache-Control': 'no-store'
                },
                cache: 'no-store',
                signal: AbortSignal.timeout(3500)
            });
            if (res.ok) {
                const data = await res.json();
                if (data && data.result && typeof data.result === 'string' && data.result.startsWith('http')) {
                    activeBackendUrl = data.result;
                    writeTmpBackendUrl(activeBackendUrl);
                    return activeBackendUrl;
                }
            }
        } catch (err) {
            console.error('[WARN] Failed to read activeBackendUrl from Upstash Redis:', err.message);
        }
    }
    if (activeBackendUrl && activeBackendUrl.startsWith('http')) {
        return activeBackendUrl;
    }
    const tmpUrl = readTmpBackendUrl();
    if (tmpUrl) {
        activeBackendUrl = tmpUrl;
        return activeBackendUrl;
    }
    return DEFAULT_BACKEND_URL;
}

async function setStoredBackendUrl(url) {
    activeBackendUrl = url;
    writeTmpBackendUrl(url);
    const { redisUrl, redisToken } = getUpstashConfig();
    if (redisUrl && redisToken) {
        try {
            await fetch(redisUrl, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${redisToken}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(['SET', 'activeBackendUrl', url, 'EX', 43200]),
                signal: AbortSignal.timeout(3500)
            });
            console.log(`[INFO] 💾 Saved activeBackendUrl to Upstash Redis: ${url}`);
        } catch (err) {
            console.error('[WARN] Failed to write activeBackendUrl to Upstash Redis:', err.message);
        }
    }
}

async function getStoredKernelLog() {
    const { redisUrl, redisToken } = getUpstashConfig();
    if (redisUrl && redisToken) {
        try {
            const res = await fetch(`${redisUrl}/get/kernelLog`, {
                method: 'GET',
                headers: {
                    Authorization: `Bearer ${redisToken}`,
                    'Cache-Control': 'no-store'
                },
                cache: 'no-store',
                signal: AbortSignal.timeout(3500)
            });
            if (res.ok) {
                const data = await res.json();
                if (data && data.result) {
                    try {
                        localKernelLog = JSON.parse(data.result);
                        return localKernelLog;
                    } catch {
                        return { message: data.result };
                    }
                }
            }
        } catch (err) {
            console.error('[WARN] Failed to read kernelLog from Upstash Redis:', err.message);
        }
    }
    return localKernelLog;
}

async function setStoredKernelLog(logData) {
    localKernelLog = logData;
    const { redisUrl, redisToken } = getUpstashConfig();
    if (redisUrl && redisToken) {
        try {
            await fetch(redisUrl, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${redisToken}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(['SET', 'kernelLog', JSON.stringify(logData), 'EX', 3600]),
                signal: AbortSignal.timeout(3500)
            });
            console.log(`[INFO] 📡 Saved kernelLog to Upstash Redis:`, logData.status);
        } catch (err) {
            console.error('[WARN] Failed to write kernelLog to Upstash Redis:', err.message);
        }
    }
}

async function clearStoredBackendUrl() {
    activeBackendUrl = "";
    writeTmpBackendUrl("");
    if (localKernelLog && localKernelLog.status === 'ready') {
        localKernelLog = null;
    }
    const { redisUrl, redisToken } = getUpstashConfig();
    if (redisUrl && redisToken) {
        try {
            await fetch(redisUrl, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${redisToken}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(['DEL', 'activeBackendUrl']),
                signal: AbortSignal.timeout(3500)
            });
            console.log('[INFO] 🗑️ Cleared activeBackendUrl from Upstash Redis');
        } catch (err) {
            console.error('[WARN] Failed to delete activeBackendUrl from Upstash Redis:', err.message);
        }
    }
}

async function probeBackendAlive(targetUrl, timeoutMs = 4500) {
    if (!targetUrl || typeof targetUrl !== 'string' || !targetUrl.startsWith('http')) {
        return { online: false, statusCode: 0, latencyMs: 0 };
    }
    const base = targetUrl.replace(/\/+$/, '');
    const t0 = Date.now();
    for (const ep of ['/config', '/heartbeat']) {
        try {
            const response = await fetch(base + ep, {
                method: 'GET',
                headers: {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                    'ngrok-skip-browser-warning': 'true'
                },
                cache: 'no-store',
                signal: AbortSignal.timeout(timeoutMs)
            });
            const latencyMs = Date.now() - t0;
            if (response.ok) {
                return { online: true, statusCode: response.status, latencyMs };
            }
            if (response.status === 530 || response.status === 1033) {
                return { online: false, statusCode: response.status, latencyMs, tunnelDead: true };
            }
        } catch (err) {
            // Tiếp tục thử endpoint kế tiếp
        }
    }
    return { online: false, statusCode: 0, latencyMs: Date.now() - t0 };
}

app.use(cors());
app.use(express.json());

// Chống cache triệt để: Đảm bảo người dùng luôn tải phiên bản Frontend mới nhất 100%
app.use((req, res, next) => {
    res.set('Cache-Control', 'no-store, no-cache, must-revalidate, proxy-revalidate');
    res.set('Pragma', 'no-cache');
    res.set('Expires', '0');
    res.set('Surrogate-Control', 'no-store');
    next();
});

app.use(express.static(path.join(__dirname, 'public'), {
    etag: false,
    lastModified: false,
    maxAge: 0
}));

// Dynamic backend update endpoint (called automatically by Kaggle GPU notebook on boot & heartbeat)
let activeBackendVersion = "";
const legacyUpgradeTriggeredMap = new Map();

app.post('/api/backend-url', async (req, res) => {
    const { url, version } = req.body || {};
    if (url && typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))) {
        const cleanUrl = url.trim().replace(/\/+$/, '');
        // Không nhận nhầm URL api.trycloudflare.com
        if (cleanUrl.includes('api.trycloudflare.com')) {
            return res.status(400).json({ error: 'Ignored internal api.trycloudflare.com URL' });
        }

        const currentUrl = (await getStoredBackendUrl() || '').trim().replace(/\/+$/, '');
        const storedLog = await getStoredKernelLog();
        const storedVersion = (storedLog && storedLog.version) || activeBackendVersion;

        // CHỈ CHẤP NHẬN PHIÊN BẢN MỚI NHẤT v8_vn_vehicle_weights: Từ chối tuyệt đối mọi heartbeat từ các kernel cũ (v1..v7)
        if (version !== 'v8_vn_vehicle_weights') {
            return res.json({ success: true, activeBackendUrl: currentUrl, ignoredLegacy: true });
        }

        // Nếu phiên mới là v8_vn_vehicle_weights còn phiên đang lưu chưa phải v8_vn_vehicle_weights -> ưu tiên nâng cấp ngay lập tức!
        const isUpgradeToV8 = (version === 'v8_vn_vehicle_weights' && storedVersion !== 'v8_vn_vehicle_weights');

        // STICKY SESSION LOCK TRÊN SERVER: Nếu cùng phiên bản và đã có 1 URL khác đang Online (200 OK), giữ cố định URL đó!
        if (!isUpgradeToV8 && currentUrl && currentUrl.startsWith('http') && currentUrl !== cleanUrl) {
            const probe = await probeBackendAlive(currentUrl, 2500);
            if (probe.online) {
                console.log(`[STICKY-LOCK] 🔒 Giữ cố định phiên đang chạy (${currentUrl}), bỏ qua URL trùng lặp (${cleanUrl})`);
                return res.json({ success: true, activeBackendUrl: currentUrl, keptExistingSession: true });
            }
        }

        if (version === 'v8_vn_vehicle_weights') {
            activeBackendVersion = 'v8_vn_vehicle_weights';
        }
        await setStoredBackendUrl(cleanUrl);
        await setStoredKernelLog({
            status: 'ready',
            message: 'Backend GPU đã sẵn sàng và kết nối thành công!',
            details: cleanUrl,
            version: version || 'v1',
            updatedAt: new Date().toISOString()
        });
        console.log(`[INFO] 🔥 Live AI Backend URL updated to: ${cleanUrl} (${version || 'v1'})`);
        return res.json({ success: true, activeBackendUrl: cleanUrl });
    }
    res.status(400).json({ error: "Invalid URL" });
});

// Telemetry & Error Logging Endpoint from Kaggle Runner & Notebook
app.post('/api/kernel-log', async (req, res) => {
    const { status, message, details, account, timestamp } = req.body || {};
    const logData = {
        status: status || 'info',
        message: message || '',
        details: details || null,
        account: account || null,
        updatedAt: timestamp || new Date().toISOString()
    };
    await setStoredKernelLog(logData);
    return res.json({ success: true, kernelLog: logData });
});

// API Config Endpoint
app.get('/api/config', async (req, res) => {
    let liveUrl = await getStoredBackendUrl();
    const kernelLog = await getStoredKernelLog();
    if ((!liveUrl || !liveUrl.startsWith('http')) && kernelLog && kernelLog.status === 'ready' && kernelLog.details && typeof kernelLog.details === 'string' && kernelLog.details.startsWith('http') && !kernelLog.details.includes('kaggle.com')) {
        liveUrl = kernelLog.details.trim().replace(/\/+$/, '');
        activeBackendUrl = liveUrl;
        writeTmpBackendUrl(liveUrl);
    }
    res.json({
        defaultBackendUrl: liveUrl,
        wakeGpuConfigured: true,
        kernelLog: kernelLog
    });
});

// ============================================================================
// CAMERA PRESETS PERSISTENCE API (LƯU CỐ ĐỊNH MỐC & LÀN ĐƯỜNG CHO TỪNG CAMERA)
// ============================================================================
let localCameraPresets = {};
const TMP_CAMERA_PRESETS_FILE = '/tmp/synexan_camera_presets.json';
const REPO_CAMERA_PRESETS_FILE = path.join(__dirname, '..', 'scripts', 'camera_presets.json');

async function getStoredCameraPresets() {
    let merged = {};
    try {
        if (fs.existsSync(REPO_CAMERA_PRESETS_FILE)) {
            const repoData = JSON.parse(fs.readFileSync(REPO_CAMERA_PRESETS_FILE, 'utf8'));
            if (repoData && typeof repoData === 'object') {
                merged = { ...merged, ...repoData };
            }
        }
    } catch (e) {}

    try {
        if (fs.existsSync(TMP_CAMERA_PRESETS_FILE)) {
            const tmpData = JSON.parse(fs.readFileSync(TMP_CAMERA_PRESETS_FILE, 'utf8'));
            if (tmpData && typeof tmpData === 'object') {
                merged = { ...merged, ...tmpData };
            }
        }
    } catch (e) {}

    if (localCameraPresets && typeof localCameraPresets === 'object') {
        merged = { ...merged, ...localCameraPresets };
    }

    const { redisUrl, redisToken } = getUpstashConfig();
    if (redisUrl && redisToken) {
        try {
            const response = await fetch(`${redisUrl}/get/cameraPresets`, {
                headers: { Authorization: `Bearer ${redisToken}` },
                cache: 'no-store',
                signal: AbortSignal.timeout(3500)
            });
            if (response.ok) {
                const data = await response.json();
                if (data && data.result) {
                    const parsed = typeof data.result === 'string' ? JSON.parse(data.result) : data.result;
                    if (parsed && typeof parsed === 'object') {
                        merged = { ...merged, ...parsed };
                    }
                }
            }
        } catch (err) {
            console.error('[WARN] Failed to read cameraPresets from Upstash Redis:', err.message);
        }
    }
    localCameraPresets = merged;
    return merged;
}

async function setStoredCameraPresets(presetsObj) {
    localCameraPresets = presetsObj || {};
    try {
        fs.writeFileSync(TMP_CAMERA_PRESETS_FILE, JSON.stringify(localCameraPresets, null, 2), 'utf8');
    } catch (e) {}

    const { redisUrl, redisToken } = getUpstashConfig();
    if (redisUrl && redisToken) {
        try {
            await fetch(redisUrl, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${redisToken}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(['SET', 'cameraPresets', JSON.stringify(localCameraPresets)]),
                signal: AbortSignal.timeout(3500)
            });
            console.log('[INFO] 💾 Saved cameraPresets to Upstash Redis');
        } catch (err) {
            console.error('[WARN] Failed to write cameraPresets to Upstash Redis:', err.message);
        }
    }
}

app.get('/api/camera-presets', async (req, res) => {
    const presets = await getStoredCameraPresets();
    return res.json({ success: true, presets });
});

app.post('/api/camera-presets', async (req, res) => {
    const body = req.body || {};
    const currentPresets = await getStoredCameraPresets();
    if (body.cameraKey && typeof body.cameraKey === 'string') {
        const key = body.cameraKey.trim();
        const existingEntry = currentPresets[key] || {};
        currentPresets[key] = {
            ...existingEntry,
            cameraKey: key,
            cameraUrl: body.cameraUrl || existingEntry.cameraUrl || key,
            cameraName: body.cameraName || existingEntry.cameraName || '',
            gauges: Array.isArray(body.gauges) ? body.gauges : (existingEntry.gauges || []),
            roi_points_norm: body.roi_points_norm !== undefined ? body.roi_points_norm : (existingEntry.roi_points_norm || null),
            roi_label: body.roi_label || existingEntry.roi_label || 'Làn đường giám sát',
            updatedAt: new Date().toISOString()
        };
        await setStoredCameraPresets(currentPresets);
        return res.json({ success: true, cameraKey: key, entry: currentPresets[key], presets: currentPresets });
    }
    if (body.presets && typeof body.presets === 'object') {
        const updated = { ...currentPresets, ...body.presets };
        await setStoredCameraPresets(updated);
        return res.json({ success: true, presets: updated });
    }
    return res.status(400).json({ error: 'Missing cameraKey or presets object' });
});

// Server-side Heartbeat Status Check
app.get('/api/status', async (req, res) => {
    const currentUrl = await getStoredBackendUrl();
    const targetUrl = (req.query.target || currentUrl || '').trim().replace(/\/+$/, '');
    if (!targetUrl || !targetUrl.startsWith('http')) {
        return res.json({ online: false, error: 'Chưa có URL Backend nào được cấu hình' });
    }

    const probe = await probeBackendAlive(targetUrl, 4500);

    // Nếu URL đang lưu trong Redis thực sự là tunnel đã đóng (HTTP 530 / 1033), tự động dọn sạch để đón URL mới
    if (!probe.online && probe.tunnelDead && currentUrl && targetUrl === currentUrl.replace(/\/+$/, '')) {
        console.log(`[INFO] 🧹 Tunnel cũ (${targetUrl}) trả về HTTP ${probe.statusCode} (đã đóng). Đang xóa khỏi Redis...`);
        await clearStoredBackendUrl();
    }

    return res.json({
        online: probe.online,
        statusCode: probe.statusCode,
        latencyMs: probe.latencyMs,
        activeBackendUrl: probe.online ? targetUrl : ""
    });
});

// Endpoint bảo mật kích hoạt GitHub Actions tự động xoay vòng & khởi động GPU Kaggle
let lastWakeTime = 0;
app.post('/api/wake-gpu', async (req, res) => {
    try {
        const isForce = !!(req.body && (req.body.force === true || req.body.force === 'true'));

        // 1. Bảo vệ tuyệt đối phiên đang chạy: Nếu Backend hiện tại đang Online 200 OK, trả về luôn, KHÔNG gọi GitHub Actions!
        const currentUrl = await getStoredBackendUrl();
        if (currentUrl && !isForce) {
            const probe = await probeBackendAlive(currentUrl, 4000);
            if (probe.online) {
                console.log(`[INFO] ✅ Backend GPU đang Online tại ${currentUrl}. Bỏ qua lệnh wake-gpu để bảo vệ phiên đang chạy!`);
                return res.json({
                    success: true,
                    alreadyOnline: true,
                    activeBackendUrl: currentUrl,
                    message: 'Backend GPU đang hoạt động sẵn sàng! Đang kết nối trực tiếp...'
                });
            }
        }

        const now = Date.now();
        if (!isForce && now - lastWakeTime < 30000) {
            console.log('[INFO] ⏳ Wake GPU request ignored (rate limit: 30s cooldown).');
            return res.json({
                success: true,
                message: 'Yêu cầu khởi động GPU đang được xử lý, vui lòng chờ trong giây lát...',
                cooldown: true
            });
        }
        lastWakeTime = now;

        const { ghToken, ghRepo } = getGitHubCredentials();

        if (!ghToken) {
            return res.status(503).json({
                success: false,
                error: 'Chưa cấu hình GH_PAT trên Environment Variables để kích hoạt GitHub Actions tự động.'
            });
        }

        if (isForce) {
            await clearStoredBackendUrl();
        }

        const ghRes = await fetch(`https://api.github.com/repos/${ghRepo}/dispatches`, {
            method: 'POST',
            headers: {
                'Accept': 'application/vnd.github.v3+json',
                'Authorization': `Bearer ${ghToken}`,
                'User-Agent': 'FrontendFlood-Vercel-Serverless',
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                event_type: 'wake-kaggle',
                client_payload: {
                    triggered_by: 'vercel_web',
                    force: isForce ? 'true' : 'false'
                }
            })
        });

        if (ghRes.status === 204 || ghRes.ok) {
            return res.json({
                success: true,
                message: 'Đã gửi tín hiệu kích hoạt GPU Backend đến GitHub Actions thành công! Hệ thống đang tự động kiểm tra quota và khởi động GPU.'
            });
        } else {
            const errBody = await ghRes.text();
            return res.status(ghRes.status).json({
                success: false,
                error: `GitHub API trả về mã ${ghRes.status}: ${errBody}`
            });
        }
    } catch (err) {
        return res.status(500).json({ success: false, error: err.message });
    }
});

// Endpoint chủ động tắt máy chủ GPU tức thì theo yêu cầu người dùng
app.post('/api/backend-shutdown', async (req, res) => {
    try {
        const currentUrl = await getStoredBackendUrl();
        if (currentUrl) {
            console.log(`[INFO] 🛑 Đang gửi tín hiệu dừng máy chủ GPU tới: ${currentUrl}/shutdown`);
            await fetch(`${currentUrl}/shutdown`, {
                method: 'POST',
                signal: AbortSignal.timeout(3500)
            }).catch(() => {});
        }
        await clearStoredBackendUrl();
        return res.json({ success: true, message: "Đã gửi tín hiệu tắt máy chủ GPU thành công." });
    } catch (err) {
        return res.status(500).json({ error: err.message });
    }
});

// Fallback all routes to index.html for SPA (No cache)
app.get('*', (req, res) => {
    const publicIndex = path.join(__dirname, 'public', 'index.html');
    const rootIndex = path.join(__dirname, 'index.html');
    const targetFile = fs.existsSync(publicIndex) ? publicIndex : rootIndex;
    res.set('Cache-Control', 'no-store, no-cache, must-revalidate, proxy-revalidate');
    res.sendFile(targetFile);
});

if (process.env.NODE_ENV !== 'test' && !process.env.VERCEL) {
    app.listen(PORT, '0.0.0.0', () => {
        console.log(`🚀 Flood Demo Frontend Server running on port ${PORT}`);
        console.log(`📡 Default AI Backend target: ${DEFAULT_BACKEND_URL || 'Upstash Redis Managed'}`);
    });
}

module.exports = app;
