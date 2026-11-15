// ==============================================================================
// SYNEXAN AI UI & STREAM CONTROLLER (NO ICONS, SINGLE FPS BADGE, MIN STRIP WIDTH)
// ==============================================================================
// 1. Giữ nguyên bố cục 2 màn hình riêng biệt (Màn hình nạp bên trái + Màn hình dự đoán bên phải).
// 2. Loại bỏ 100% các icon/emoji trên toàn bộ giao diện Gradio.
// 3. Hiển thị duy nhất 1 badge FPS & Delay trên tiêu đề màn hình bên phải (không lặp chữ FPS, không lặp 2 badge).
// 4. Đưa mặc định thanh trượt "Bề rộng cột mốc" về mức nhỏ nhất (min = 4).
// 5. Đảm bảo chữ tiêu đề .header-box luôn có màu trắng sáng (#ffffff) nổi bật trên nền xanh đậm.

(function () {
  'use strict';

  console.log('[Synexan UI Controller v4] Khởi động giao diện chuẩn (1 FPS Badge, Min Strip Width = 4, White Header)...');

  let globalLatestFps = 0;
  let globalLatestLatency = 71;
  let frameTimestamps = [];
  let lastFrameSrcByCanvas = {};
  let lastIdleResetTimer = null;

  // ----------------------------------------------------------------------------
  // 0. CSS TIÊM TRỰC TIẾP: SỬA MÀU CHỮ TIÊU ĐỀ TRẮNG SÁNG & CHỐNG LẶP BADGE
  // ----------------------------------------------------------------------------
  function injectCustomStyles() {
    if (document.getElementById('synexan-v4-custom-styles')) return;
    const style = document.createElement('style');
    style.id = 'synexan-v4-custom-styles';
    style.textContent = `
      /* Đảm bảo tiêu đề chính và mô tả trong .header-box luôn trắng sáng trên nền xanh đậm */
      .header-box,
      .header-box h1,
      .header-box p,
      .header-box b,
      .header-box strong,
      .header-box span,
      .prose .header-box h1,
      .prose .header-box p,
      .prose .header-box b,
      .prose .header-box strong {
        color: #ffffff !important;
        text-shadow: 0 1px 3px rgba(0, 0, 0, 0.28) !important;
      }
      /* Ẩn hoàn toàn mọi badge phụ trên góc canvas để không bao giờ bị nhân đôi FPS */
      .synexan-hud-perf-badge {
        display: none !important;
      }
    `;
    (document.head || document.documentElement).appendChild(style);
  }

  // ----------------------------------------------------------------------------
  // 1. LOẠI BỎ SẠCH 100% ICON / EMOJI TRÊN MỌI PHẦN TỬ GRADIO
  // ----------------------------------------------------------------------------
  const EMOJI_REGEX = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{1F600}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{FE0F}\u{200D}🌊⚡🚗📁🖼️🚀⏹⚙️🗑🎯🚦📋📍💡📏🔄✅❌⚠️🛑🎉⏳🔎📌]/gu;

  function sanitizeAllGradioEmojis() {
    const selectors = 'h1, h2, h3, h4, h5, h6, button, span, label, p, b, strong, td, th, .tab-nav button, .prose *, [role="tab"]';
    const elements = document.querySelectorAll(selectors);
    elements.forEach((el) => {
      if (el.classList && el.classList.contains('synexan-header-perf-badge')) return;
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
  // 2. CẬP NHẬT THÔNG SỐ FPS & DELAY (DUY NHẤT 1 BADGE, KHÔNG LẶP CHỮ FPS)
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
    return `FPS: ${globalLatestFps} | Delay: ${globalLatestLatency}ms`;
  }

  function updateAllPerfBadges() {
    const text = getPerfText();
    const isStreaming = globalLatestFps > 0;

    const badges = document.querySelectorAll('.synexan-header-perf-badge');
    badges.forEach((b) => {
      b.textContent = text;
      b.style.color = isStreaming ? '#34d399' : '#38bdf8';
      b.style.borderColor = isStreaming ? 'rgba(52, 211, 153, 0.45)' : 'rgba(56, 189, 248, 0.35)';
    });
  }

  function attachHeaderPerfBadges() {
    // Xóa mọi badge HUD cũ nếu còn sót lại
    document.querySelectorAll('.synexan-hud-perf-badge').forEach((el) => el.remove());

    const headings = document.querySelectorAll('.prose h4, h4');
    headings.forEach((el) => {
      // Lấy nội dung văn bản gốc (không tính nội dung của badge bên trong)
      let baseText = '';
      el.childNodes.forEach((node) => {
        if (node.nodeType === Node.TEXT_NODE) {
          baseText += node.nodeValue || '';
        } else if (node.nodeType === Node.ELEMENT_NODE && !node.classList.contains('synexan-header-perf-badge')) {
          baseText += node.textContent || '';
        }
      });

      if (
        baseText.includes('Màn Hình Video') ||
        baseText.includes('Màn Hình Giám Sát')
      ) {
        el.style.display = 'flex';
        el.style.justifyContent = 'space-between';
        el.style.alignItems = 'center';
        el.style.width = '100%';
        el.style.flexWrap = 'wrap';
        el.style.gap = '8px';

        const existingBadges = el.querySelectorAll('.synexan-header-perf-badge');
        if (existingBadges.length > 1) {
          for (let i = 1; i < existingBadges.length; i++) {
            existingBadges[i].remove();
          }
        }

        if (existingBadges.length === 0) {
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
  // 3. ĐƯA MẶC ĐỊNH THANH TRƯỢT "BỀ RỘNG CỘT MỐC" VỀ MỨC NHỎ NHẤT (MIN = 4)
  // ----------------------------------------------------------------------------
  function patchGradioConfigMinStripWidth() {
    const cfg = window.gradio_config;
    if (cfg && Array.isArray(cfg.components) && !cfg.__synexanPatchedStripWidth) {
      cfg.__synexanPatchedStripWidth = true;
      cfg.components.forEach((c) => {
        if (c && c.props && typeof c.props.label === 'string' && c.props.label.includes('Bề rộng cột mốc')) {
          c.props.value = c.props.minimum || 4;
        }
        if (c && c.type === 'state' && c.props && c.props.value && typeof c.props.value === 'object' && 'strip_width' in c.props.value) {
          c.props.value.strip_width = 4;
        }
      });
    }
  }

  function enforceMinStripWidthOnLoad() {
    patchGradioConfigMinStripWidth();
    const labels = document.querySelectorAll('label, span[data-testid="block-info"]');
    labels.forEach((lbl) => {
      const txt = (lbl.textContent || '').trim();
      if (txt.includes('Bề rộng cột mốc')) {
        const container = lbl.closest('.block, .form, div');
        if (!container || container.__synexanMinStripApplied) return;

        const rangeInput = container.querySelector('input[type="range"]');
        const numInput = container.querySelector('input[type="number"]');
        if (rangeInput) {
          const minVal = rangeInput.min || '4';
          container.__synexanMinStripApplied = true;
          if (String(rangeInput.value) === '20') {
            const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
            if (nativeSetter) {
              nativeSetter.call(rangeInput, minVal);
            } else {
              rangeInput.value = minVal;
            }
            rangeInput.dispatchEvent(new Event('input', { bubbles: true }));
            rangeInput.dispatchEvent(new Event('change', { bubbles: true }));
          }
          if (numInput && String(numInput.value) === '20') {
            const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
            if (nativeSetter) {
              nativeSetter.call(numInput, minVal);
            } else {
              numInput.value = minVal;
            }
            numInput.dispatchEvent(new Event('input', { bubbles: true }));
            numInput.dispatchEvent(new Event('change', { bubbles: true }));
          }
        }
      }
    });
  }

  // ----------------------------------------------------------------------------
  // 4. THEO DÕI MÀN HÌNH DỰ ĐOÁN BÊN PHẢI ĐỂ ĐO FPS THỰC TẾ
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

  function checkCanvasFrameUpdate(canvasId) {
    const canvasElem = document.getElementById(canvasId);
    if (!canvasElem) return;

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
    injectCustomStyles();
    sanitizeAllGradioEmojis();
    attachHeaderPerfBadges();
    enforceMinStripWidthOnLoad();
    attachCanvasObservers();
  }

  setInterval(masterTick, 300);
  window.addEventListener('DOMContentLoaded', masterTick);
  window.addEventListener('load', masterTick);
})();
