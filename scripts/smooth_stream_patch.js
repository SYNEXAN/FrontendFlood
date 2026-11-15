// ==============================================================================
// SYNEXAN AI UI & STREAM CONTROLLER (NO ICONS, LIVE FPS/DELAY, DUAL-SCREEN)
// ==============================================================================
// 1. Giữ nguyên bố cục 2 màn hình riêng biệt như ban đầu (Màn hình nạp bên trái + Màn hình dự đoán bên phải).
// 2. Loại bỏ 100% các icon/emoji trên toàn bộ giao diện Gradio.
// 3. Bổ sung thông số FPS & Delay (Độ trễ ms) thời gian thực trên tiêu đề và góc màn hình dự đoán.

(function () {
  'use strict';

  console.log('[Synexan UI Controller] Khởi động bộ điều khiển giao diện 2 màn hình & FPS/Delay HUD...');

  let globalLatestFps = 0;
  let globalLatestLatency = 71;
  let frameTimestamps = [];
  let lastFrameSrcByCanvas = {};
  let lastIdleResetTimer = null;

  // ----------------------------------------------------------------------------
  // 1. LOẠI BỎ SẠCH 100% ICON / EMOJI TRÊN MỌI PHẦN TỬ GRADIO
  // ----------------------------------------------------------------------------
  const EMOJI_REGEX = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{1F600}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{FE0F}\u{200D}🌊⚡🚗📁🖼️🚀⏹⚙️🗑🎯🚦📋📍💡📏🔄✅❌⚠️🛑🎉⏳🔎📌]/gu;

  function sanitizeAllGradioEmojis() {
    const selectors = 'h1, h2, h3, h4, h5, h6, button, span, label, p, b, strong, td, th, .tab-nav button, .prose *, [role="tab"]';
    const elements = document.querySelectorAll(selectors);
    elements.forEach((el) => {
      if (el.childNodes && el.childNodes.length > 0) {
        el.childNodes.forEach((node) => {
          if (node.nodeType === Node.TEXT_NODE && node.nodeValue) {
            EMOJI_REGEX.lastIndex = 0;
            if (EMOJI_REGEX.test(node.nodeValue)) {
              EMOJI_REGEX.lastIndex = 0;
              node.nodeValue = node.nodeValue.replace(EMOJI_REGEX, '').replace(/^\s+/, '');
            }
          }
        });
      }
    });
  }

  // ----------------------------------------------------------------------------
  // 2. CẬP NHẬT THÔNG SỐ FPS & DELAY (ĐỘ TRỄ) THỜI GIAN THỰC
  // ----------------------------------------------------------------------------
  function recordNewFrameAndComputeFps() {
    const now = performance.now();
    frameTimestamps.push(now);
    while (frameTimestamps.length > 0 && now - frameTimestamps[0] > 1000) {
      frameTimestamps.shift();
    }
    globalLatestFps = Math.min(60, Math.max(1, frameTimestamps.length));
    updateAllPerfBadges();

    clearTimeout(lastIdleResetTimer);
    lastIdleResetTimer = setTimeout(() => {
      globalLatestFps = 0;
      frameTimestamps = [];
      updateAllPerfBadges();
    }, 2200);
  }

  function getPerfText() {
    const fpsDisplay = globalLatestFps > 0 ? `${globalLatestFps} FPS` : `0 FPS (Standby)`;
    return `FPS: ${fpsDisplay} | Delay: ${globalLatestLatency}ms`;
  }

  function updateAllPerfBadges() {
    const text = getPerfText();
    const isStreaming = globalLatestFps > 0;

    const badges = document.querySelectorAll('.synexan-header-perf-badge, .synexan-hud-perf-badge');
    badges.forEach((b) => {
      b.textContent = text;
      b.style.color = isStreaming ? '#34d399' : '#38bdf8';
      b.style.borderColor = isStreaming ? 'rgba(52, 211, 153, 0.45)' : 'rgba(56, 189, 248, 0.35)';
    });
  }

  function attachHeaderPerfBadges() {
    const headings = document.querySelectorAll('h1, h2, h3, h4, h5, h6, .prose h4');
    headings.forEach((el) => {
      const text = el.textContent || '';
      if (
        text.includes('Màn Hình Video') ||
        text.includes('Màn Hình Giám Sát')
      ) {
        el.style.display = 'flex';
        el.style.justifyContent = 'space-between';
        el.style.alignItems = 'center';
        el.style.width = '100%';
        el.style.flexWrap = 'wrap';
        el.style.gap = '8px';

        if (!el.querySelector('.synexan-header-perf-badge')) {
          const badge = document.createElement('span');
          badge.className = 'synexan-header-perf-badge';
          badge.style.cssText = `
            font-size: 12px;
            font-weight: 700;
            color: #38bdf8;
            background: rgba(15, 23, 42, 0.9);
            border: 1px solid rgba(56, 189, 248, 0.35);
            border-radius: 6px;
            padding: 4px 12px;
            letter-spacing: 0.3px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
            user-select: none;
            white-space: nowrap;
          `;
          badge.textContent = getPerfText();
          el.appendChild(badge);
        }
      }
    });
  }

  // Lắng nghe cập nhật Ping / Delay từ trang mẹ Vercel
  window.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'PING_UPDATE') {
      const pingVal = event.data.latencyMs;
      if (typeof pingVal === 'number' && pingVal > 0) {
        globalLatestLatency = pingVal;
        updateAllPerfBadges();
      }
    }
  });

  // ----------------------------------------------------------------------------
  // 3. THEO DÕI MÀN HÌNH DỰ ĐOÁN BÊN PHẢI ĐỂ ĐO FPS & HIỂN THỊ HUD
  // ----------------------------------------------------------------------------
  const CANVAS_IDS = ['tab5_calib_canvas', 'traffic_canvas', 'fusion_canvas'];

  function getCanvasImageSrc(canvasElem) {
    if (!canvasElem) return '';
    const img = canvasElem.querySelector('img');
    if (img) {
      const s = img.currentSrc || img.src || img.getAttribute('src') || '';
      if (s && s.length > 20) return s;
    }
    return '';
  }

  function ensureCanvasHudBadge(canvasElem) {
    if (!canvasElem) return;
    if (getComputedStyle(canvasElem).position === 'static') {
      canvasElem.style.position = 'relative';
    }
    if (!canvasElem.querySelector('.synexan-hud-perf-badge')) {
      const hudBadge = document.createElement('div');
      hudBadge.className = 'synexan-hud-perf-badge';
      hudBadge.style.cssText = `
        position: absolute;
        top: 10px;
        right: 10px;
        background: rgba(15, 23, 42, 0.88);
        color: #38bdf8;
        border: 1px solid rgba(56, 189, 248, 0.4);
        font-size: 12px;
        font-weight: 700;
        padding: 4px 10px;
        border-radius: 6px;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45);
        z-index: 25;
        pointer-events: none;
        letter-spacing: 0.3px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      `;
      hudBadge.textContent = getPerfText();
      canvasElem.appendChild(hudBadge);
    }
  }

  function checkCanvasFrameUpdate(canvasId) {
    const canvasElem = document.getElementById(canvasId);
    if (!canvasElem) return;

    ensureCanvasHudBadge(canvasElem);

    const outputSrc = getCanvasImageSrc(canvasElem);
    if (outputSrc && lastFrameSrcByCanvas[canvasId] !== outputSrc) {
      if (lastFrameSrcByCanvas[canvasId]) {
        recordNewFrameAndComputeFps();
      }
      lastFrameSrcByCanvas[canvasId] = outputSrc;
    }
  }

  function attachCanvasObservers() {
    CANVAS_IDS.forEach((canvasId) => {
      const canvasElem = document.getElementById(canvasId);
      if (canvasElem && !canvasElem.__synexanObserved) {
        canvasElem.__synexanObserved = true;
        const obs = new MutationObserver(() => {
          checkCanvasFrameUpdate(canvasId);
        });
        obs.observe(canvasElem, {
          childList: true,
          subtree: true,
          attributes: true,
          attributeFilter: ['src']
        });
      }
      checkCanvasFrameUpdate(canvasId);
    });
  }

  function masterTick() {
    sanitizeAllGradioEmojis();
    attachHeaderPerfBadges();
    attachCanvasObservers();
  }

  setInterval(masterTick, 300);
  window.addEventListener('DOMContentLoaded', masterTick);
  window.addEventListener('load', masterTick);
})();
