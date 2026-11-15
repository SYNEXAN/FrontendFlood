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
// GITHUB & PERSISTENT STORAGE HELPERS
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

const LOCAL_BACKEND_JSON_PATHS = [
    path.join(__dirname, '..', 'backend-url.json'),
    path.join(__dirname, 'backend-url.json')
];

// Nạp URL từ backend-url.json cục bộ khi khởi động (nếu có)
for (const p of LOCAL_BACKEND_JSON_PATHS) {
    if (fs.existsSync(p)) {
        try {
            const raw = fs.readFileSync(p, 'utf8');
            const parsed = JSON.parse(raw);
            if (parsed && parsed.activeBackendUrl && parsed.activeBackendUrl.startsWith('http')) {
                activeBackendUrl = parsed.activeBackendUrl;
                console.log(`[INFO] 📄 Nạp activeBackendUrl từ file ${p}: ${activeBackendUrl}`);
                break;
            }
        } catch (e) {}
    }
}

async function persistBackendUrlToGitHub(url) {
    const { ghToken, ghRepo } = getGitHubCredentials();
    if (!ghToken) return;

    try {
        const fileUrl = `https://api.github.com/repos/${ghRepo}/contents/backend-url.json`;
        const headers = {
            'Authorization': `Bearer ${ghToken}`,
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'FrontendFlood-Persistence'
        };

        let sha = null;
        try {
            const getRes = await fetch(fileUrl, { headers, signal: AbortSignal.timeout(3500) });
            if (getRes.ok) {
                const fileData = await getRes.json();
                sha = fileData.sha;
            }
        } catch (e) {}

        const contentObj = {
            activeBackendUrl: url,
            updatedAt: new Date().toISOString(),
            source: 'heartbeat_auto_sync'
        };
        const encodedContent = Buffer.from(JSON.stringify(contentObj, null, 2)).toString('base64');

        const putBody = {
            message: `chore: sync live backend url ${url} [skip ci]`,
            content: encodedContent
        };
        if (sha) putBody.sha = sha;

        const putRes = await fetch(fileUrl, {
            method: 'PUT',
            headers: {
                ...headers,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(putBody),
            signal: AbortSignal.timeout(6000)
        });

        if (putRes.ok) {
            console.log(`[INFO] 🚀 Đã lưu persistent backend-url.json lên GitHub thành công!`);
        } else {
            console.warn(`[WARN] GitHub Contents API trả về HTTP ${putRes.status}`);
        }
    } catch (err) {
        console.error('[WARN] Failed to persist backend URL to GitHub:', err.message);
    }
}

// ============================================================================
// UPSTASH REDIS REST API & GITHUB PERSISTENT CLIENT
// ============================================================================
async function getStoredBackendUrl() {
    if (activeBackendUrl && activeBackendUrl.startsWith('http')) {
        return activeBackendUrl;
    }

    const redisUrl = process.env.UPSTASH_REDIS_REST_URL;
    const redisToken = process.env.UPSTASH_REDIS_REST_TOKEN;
    if (redisUrl && redisToken) {
        try {
            const res = await fetch(`${redisUrl}/get/activeBackendUrl`, {
                method: 'GET',
                headers: { Authorization: `Bearer ${redisToken}` },
                signal: AbortSignal.timeout(3500)
            });
            if (res.ok) {
                const data = await res.json();
                if (data && data.result) {
                    activeBackendUrl = data.result;
                    return activeBackendUrl;
                }
            }
        } catch (err) {
            console.error('[WARN] Failed to read activeBackendUrl from Upstash Redis:', err.message);
        }
    }

    // Đọc từ raw GitHub nếu Vercel vừa cold-start
    const { ghRepo } = getGitHubCredentials();
    if (ghRepo) {
        try {
            const rawRes = await fetch(`https://raw.githubusercontent.com/${ghRepo}/main/backend-url.json?t=${Date.now()}`, {
                signal: AbortSignal.timeout(3500),
                cache: 'no-store'
            });
            if (rawRes.ok) {
                const rawData = await rawRes.json();
                if (rawData && rawData.activeBackendUrl && rawData.activeBackendUrl.startsWith('http')) {
                    activeBackendUrl = rawData.activeBackendUrl;
                    console.log(`[INFO] 🌐 Nạp activeBackendUrl từ GitHub Raw: ${activeBackendUrl}`);
                    return activeBackendUrl;
                }
            }
        } catch (err) {}
    }

    return activeBackendUrl || DEFAULT_BACKEND_URL;
}

async function setStoredBackendUrl(url) {
    activeBackendUrl = url;

    // 1. Lưu cục bộ nếu hệ thống file cho phép ghi
    for (const p of LOCAL_BACKEND_JSON_PATHS) {
        try {
            fs.writeFileSync(p, JSON.stringify({ activeBackendUrl: url, updatedAt: new Date().toISOString() }, null, 2));
        } catch (e) {}
    }

    // 2. Lưu vào Upstash Redis nếu có
    const redisUrl = process.env.UPSTASH_REDIS_REST_URL;
    const redisToken = process.env.UPSTASH_REDIS_REST_TOKEN;
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

    // 3. Bất đồng bộ đồng bộ lên GitHub backend-url.json (Zero Data Loss)
    persistBackendUrlToGitHub(url).catch(() => {});
}

async function getStoredKernelLog() {
    const redisUrl = process.env.UPSTASH_REDIS_REST_URL;
    const redisToken = process.env.UPSTASH_REDIS_REST_TOKEN;
    if (redisUrl && redisToken) {
        try {
            const res = await fetch(`${redisUrl}/get/kernelLog`, {
                method: 'GET',
                headers: { Authorization: `Bearer ${redisToken}` },
                signal: AbortSignal.timeout(3500)
            });
            if (res.ok) {
                const data = await res.json();
                if (data && data.result) {
                    try {
                        return JSON.parse(data.result);
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
    const redisUrl = process.env.UPSTASH_REDIS_REST_URL;
    const redisToken = process.env.UPSTASH_REDIS_REST_TOKEN;
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
    const redisUrl = process.env.UPSTASH_REDIS_REST_URL;
    const redisToken = process.env.UPSTASH_REDIS_REST_TOKEN;
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

// Dynamic backend update endpoint (called automatically by Kaggle GPU notebook on boot)
app.post('/api/backend-url', async (req, res) => {
    const { url } = req.body;
    if (url && typeof url === 'string' && (url.startsWith('https://') || url.startsWith('http://'))) {
        const cleanUrl = url.trim().replace(/\/+$/, '');
        await setStoredBackendUrl(cleanUrl);
        await setStoredKernelLog({
            status: 'ready',
            message: 'Backend GPU đã sẵn sàng và kết nối thành công!',
            details: cleanUrl,
            updatedAt: new Date().toISOString()
        });
        console.log(`[INFO] 🔥 Live AI Backend URL updated to: ${cleanUrl}`);
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
    const liveUrl = await getStoredBackendUrl();
    const kernelLog = await getStoredKernelLog();
    res.json({
        defaultBackendUrl: liveUrl,
        wakeGpuConfigured: true,
        kernelLog: kernelLog
    });
});

// Server-side Heartbeat Status Check
app.get('/api/status', async (req, res) => {
    const currentUrl = await getStoredBackendUrl();
    const targetUrl = req.query.target || currentUrl;
    if (!targetUrl || typeof targetUrl !== 'string' || !targetUrl.startsWith('http')) {
        return res.json({ online: false, error: 'Chưa có URL Backend nào được cấu hình' });
    }

    let checkUrl = targetUrl.replace(/\/+$/, '') + '/config';
    const t0 = Date.now();

    try {
        const response = await fetch(checkUrl, {
            method: 'GET',
            headers: {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'ngrok-skip-browser-warning': 'true'
            },
            signal: AbortSignal.timeout(5000)
        });

        const latencyMs = Date.now() - t0;
        return res.json({
            online: response.ok,
            statusCode: response.status,
            latencyMs
        });
    } catch (err) {
        return res.json({
            online: false,
            error: err.name === 'TimeoutError' ? 'timeout' : err.message
        });
    }
});

// Endpoint bảo mật kích hoạt GitHub Actions tự động xoay vòng & khởi động GPU Kaggle
let lastWakeTime = 0;
app.post('/api/wake-gpu', async (req, res) => {
    try {
        const isForce = !!(req.body && (req.body.force === true || req.body.force === 'true'));
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
            console.log('[INFO] ⚡ Force restart requested by client (breaking queue stall).');
            const oldUrl = await getStoredBackendUrl();
            if (oldUrl) {
                console.log(`[CLEAN SHUTDOWN] 🛑 Tắt phiên GPU cũ tại ${oldUrl}/shutdown trước khi tạo phiên mới...`);
                try {
                    await fetch(`${oldUrl}/shutdown`, { method: 'POST', signal: AbortSignal.timeout(2000) }).catch(() => {});
                } catch (e) {}
                await clearStoredBackendUrl();
            }
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
