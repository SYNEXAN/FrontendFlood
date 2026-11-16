// ==============================================================================
// SYNEXAN AI UI & STREAM CONTROLLER (v5_root_clean: 0ms PRE-RENDER SANITIZER)
// ==============================================================================
// 1. Xóa tận gốc (Pre-Render 0ms) toàn bộ icon/emoji, dòng "Hướng dẫn gán mốc..."
//    và đặt mặc định "Bề rộng cột mốc = 4" ngay trong window.gradio_config trước khi Svelte khởi tạo.
// 2. Giữ nguyên bố cục 2 màn hình riêng biệt (Màn hình nạp bên trái + Màn hình dự đoán bên phải).
// 3. Hiển thị duy nhất 1 badge FPS & Delay trên tiêu đề màn hình bên phải (không lặp chữ FPS, không lặp 2 badge).
// 4. Đảm bảo chữ tiêu đề .header-box luôn có màu trắng sáng (#ffffff) nổi bật trên nền xanh đậm.

(function () {
  'use strict';

  const EMOJI_REGEX = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{1F600}-\u{1F64F}\u{1F680}-\u{1F6FF}\u{FE0F}\u{200D}🌊⚡🚗📁🖼️🚀⏹⚙️🗑🎯🚦📋📍💡📏🔄✅❌⚠️🛑🎉⏳🔎📌➔👉🎬📷📐]/gu;

  function cleanTextString(str) {
    if (typeof str !== 'string' || !str) return str;
    if (
      str.includes('Hướng dẫn gán mốc') ||
      str.includes('ướng dẫn gán mốc') ||
      str.includes('Hướng dẫn hiệu chuẩn') ||
      str.includes('Nhấp <b>4 góc</b> trên màn hình')
    ) {
      return '';
    }
    EMOJI_REGEX.lastIndex = 0;
    return str
      .replace(EMOJI_REGEX, '')
      .replace(/^(#+)\s*/, '$1 ')
      .replace(/(\n#+)\s*/g, '$1 ')
      .replace(/>\s+/g, '>')
      .replace(/^\s+/, '');
  }

  // ----------------------------------------------------------------------------
  // 0. XÓA TẬN GỐC TRONG CẤU HÌNH GRADIO (CHẠY TỨC THÌ 0ms TRƯỚC KHI VẼ GIAO DIỆN)
  // ----------------------------------------------------------------------------
  function sanitizeGradioConfigRoot(cfg) {
    if (!cfg || typeof cfg !== 'object') return cfg;
    if (typeof cfg.title === 'string') {
      cfg.title = cleanTextString(cfg.title);
    }
    if (Array.isArray(cfg.components)) {
      cfg.components.forEach((c) => {
        if (!c || !c.props) return;
        const p = c.props;

        // 1. Xóa tận gốc các khung hướng dẫn gán mốc / hiệu chuẩn và ẩn hoàn toàn công cụ chấm mốc
        if (
          p.elem_id === 'tab5_setup_status' ||
          p.elem_id === 'traffic_setup_status' ||
          p.elem_id === 'fusion_setup_status' ||
          p.elem_id === 'tab_item_fusion_gauge' ||
          p.elem_id === 'tab_item_fusion_lane' ||
          p.elem_id === 'fusion_seek_slider' ||
          p.elem_id === 'tab5_seek_slider' ||
          p.elem_id === 'traffic_seek_slider'
        ) {
          p.value = '';
          p.visible = false;
        }

        if (
          typeof p.label === 'string' &&
          (p.label.includes('Thiết Lập Cột Mốc') ||
            p.label.includes('Thiết Lập Khung Làn Đường') ||
            p.label.includes('Khung hình video') ||
            p.label.includes('Thông tin cột mốc') ||
            p.label.includes('Tên cột mốc') ||
            p.label.includes('Điểm đầu') ||
            p.label.includes('Điểm cuối') ||
            p.label.includes('Cột mốc đã lưu') ||
            p.label.includes('Nhãn khung làn đường'))
        ) {
          p.visible = false;
        }

        // 2. Đặt mặc định thanh trượt "Bề rộng cột mốc" về mức nhỏ nhất (min = 4)
        if (typeof p.label === 'string' && p.label.includes('Bề rộng cột mốc')) {
          p.value = p.minimum || 4;
        }
        if (c.type === 'state' && p.value && typeof p.value === 'object' && 'strip_width' in p.value) {
          p.value.strip_width = 4;
        }

        // 3. Xóa tận gốc 100% icon/emoji trong tất cả thuộc tính văn bản của Component
        ['label', 'value', 'title', 'info', 'placeholder'].forEach((key) => {
          if (typeof p[key] === 'string') {
            p[key] = cleanTextString(p[key]);
          }
        });

        if (Array.isArray(p.headers)) {
          p.headers = p.headers.map((h) => (typeof h === 'string' ? cleanTextString(h) : h));
        }

        // Làm sạch toàn bộ các hàng bảng DataFrame (khử 100% emoji trong bảng khi reload)
        if (Array.isArray(p.value)) {
          p.value = p.value.map((row) => {
            if (Array.isArray(row)) {
              return row.map((cell) => (typeof cell === 'string' ? cleanTextString(cell) : cell));
            }
            return typeof row === 'string' ? cleanTextString(row) : row;
          });
        }
        if (p.value && typeof p.value === 'object' && Array.isArray(p.value.data)) {
          p.value.data = p.value.data.map((row) => {
            if (Array.isArray(row)) {
              return row.map((cell) => (typeof cell === 'string' ? cleanTextString(cell) : cell));
            }
            return typeof row === 'string' ? cleanTextString(row) : row;
          });
        }

        if (Array.isArray(p.choices)) {
          p.choices = p.choices.map((ch) => {
            if (Array.isArray(ch) && ch.length >= 2) {
              return [typeof ch[0] === 'string' ? cleanTextString(ch[0]) : ch[0], ch[1]];
            }
            return typeof ch === 'string' ? cleanTextString(ch) : ch;
          });
        }
      });
    }
    return cfg;
  }

  // Chặn ngay khi window.gradio_config đã có sẵn hoặc sắp được gán trong <head>
  try {
    let internalGradioConfig = window.gradio_config ? sanitizeGradioConfigRoot(window.gradio_config) : undefined;
    Object.defineProperty(window, 'gradio_config', {
      configurable: true,
      enumerable: true,
      get: function () {
        return internalGradioConfig;
      },
      set: function (newVal) {
        internalGradioConfig = sanitizeGradioConfigRoot(newVal);
      }
    });
  } catch (e) {
    if (window.gradio_config) {
      sanitizeGradioConfigRoot(window.gradio_config);
    }
  }

  // Chặn luôn API fetch('/config') của Gradio để mọi dữ liệu nạp về đều sạch 100% trước khi render
  try {
    const origFetch = window.fetch;
    if (typeof origFetch === 'function') {
      window.fetch = async function (...args) {
        const res = await origFetch.apply(this, args);
        try {
          const reqUrl = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
          if (reqUrl.includes('/config')) {
            const clone = res.clone();
            const data = await clone.json();
            const cleaned = sanitizeGradioConfigRoot(data);
            return new Response(JSON.stringify(cleaned), {
              status: res.status,
              statusText: res.statusText,
              headers: res.headers
            });
          }
        } catch (err) {}
        return res;
      };
    }
  } catch (e) {}

  // ----------------------------------------------------------------------------
  // 1. CSS TIÊM NGAY TRONG <HEAD>: CHỮ TIÊU ĐỀ TRẮNG SÁNG & ẨN VĨNH VIỄN DÒNG HƯỚNG DẪN
  // ----------------------------------------------------------------------------
  function injectCustomStyles() {
    if (document.getElementById('synexan-v5-custom-styles')) return;
    const style = document.createElement('style');
    style.id = 'synexan-v5-custom-styles';
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
      /* Ẩn vĩnh viễn toàn bộ công cụ gán mốc / hiệu chuẩn thủ công và badge HUD phụ */
      #tab5_setup_status,
      #traffic_setup_status,
      #fusion_setup_status,
      #tab_item_fusion_gauge,
      #tab_item_fusion_lane,
      [id*="tab_item_fusion_gauge"],
      [id*="tab_item_fusion_lane"],
      [id*="fusion_seek_slider"],
      [id*="tab5_seek_slider"],
      [id*="traffic_seek_slider"],
      div:has(> #fusion_seek_slider),
      div:has(> #fusion_calib_tabs),
      .synexan-calibration-panel,
      .synexan-hud-perf-badge {
        display: none !important;
        visibility: hidden !important;
        height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
      }
    `;
    (document.head || document.documentElement).appendChild(style);
  }
  injectCustomStyles();

  // ----------------------------------------------------------------------------
  // 2. LOẠI BỎ SẠCH 100% ICON / EMOJI PHÁT SINH ĐỘNG KHI THAO TÁC
  // ----------------------------------------------------------------------------
  function sanitizeAllGradioEmojis() {
    const selectors = 'h1, h2, h3, h4, h5, h6, button, span, label, p, b, strong, td, th, .tab-nav button, .prose *, [role="tab"]';
    const elements = document.querySelectorAll(selectors);
    elements.forEach((el) => {
      if (el.classList && el.classList.contains('synexan-header-perf-badge')) return;
      const txt = el.textContent || '';
      if (
        txt.includes('Hướng dẫn gán mốc') ||
        txt.includes('ướng dẫn gán mốc') ||
        txt.includes('Hướng dẫn hiệu chuẩn') ||
        txt === 'Lưu' ||
        txt === 'Hủy' ||
        txt.includes('Xóa tất cả mốc') ||
        txt.includes('Xóa cột mốc') ||
        txt.includes('Xóa Khung Làn Đường') ||
        txt.includes('Thiết Lập Cột Mốc') ||
        txt.includes('Thiết Lập Khung Làn Đường') ||
        txt.includes('Thông tin cột mốc')
      ) {
        el.textContent = '';
        el.style.display = 'none';
        return;
      }
      if (el.childNodes && el.childNodes.length > 0) {
        el.childNodes.forEach((node) => {
          if (node.nodeType === Node.TEXT_NODE && node.nodeValue) {
            EMOJI_REGEX.lastIndex = 0;
            if (EMOJI_REGEX.test(node.nodeValue) || /^#{2,6}\s*/.test(node.nodeValue.trim())) {
              EMOJI_REGEX.lastIndex = 0;
              const hadHashes = /^#{2,6}\s*/.test(node.nodeValue.trim());
              node.nodeValue = node.nodeValue
                .replace(EMOJI_REGEX, '')
                .replace(/^\s*#{2,6}\s*/, '')
                .replace(/^\s+/, '');
              if (hadHashes && el.tagName === 'P') {
                el.style.fontWeight = '700';
                el.style.fontSize = '16px';
                el.style.margin = '4px 0 8px 0';
              }
            }
          }
        });
      }
    });
  }

  // ----------------------------------------------------------------------------
  // 3. CẬP NHẬT THÔNG SỐ FPS & DELAY (DUY NHẤT 1 BADGE, KHÔNG LẶP CHỮ FPS)
  // ----------------------------------------------------------------------------
  let globalLatestFps = 0;
  let globalLatestLatency = 71;
  let frameTimestamps = [];
  let lastFrameSrcByCanvas = {};
  let lastIdleResetTimer = null;

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
    document.querySelectorAll('.synexan-hud-perf-badge').forEach((el) => el.remove());

    const headings = document.querySelectorAll('.prose h4, h4');
    headings.forEach((el) => {
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
  // 4. ĐẢM BẢO THANH TRƯỢT "BỀ RỘNG CỘT MỐC" LUÔN Ở MỨC MIN = 4 KHI KHỞI ĐỘNG
  // ----------------------------------------------------------------------------
  function enforceMinStripWidthOnLoad() {
    if (window.gradio_config) {
      sanitizeGradioConfigRoot(window.gradio_config);
    }
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
  // 5. THEO DÕI MÀN HÌNH DỰ ĐOÁN BÊN PHẢI ĐỂ ĐO FPS THỰC TẾ
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

  setInterval(masterTick, 250);
  window.addEventListener('DOMContentLoaded', masterTick);
  window.addEventListener('load', masterTick);
})();
