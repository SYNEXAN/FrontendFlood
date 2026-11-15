// ==============================================================================
// SYNEXAN AI UI & STREAM CONTROLLER (NO ICONS, LIVE FPS/DELAY, SINGLE-SCREEN)
// ==============================================================================
// 1. Loại bỏ 100% các icon/emoji trên toàn bộ giao diện Gradio.
// 2. Bổ sung thông số FPS & Delay (Độ trễ ms) thời gian thực trên tiêu đề và HUD.
// 3. Gộp khung kết quả AI và khung đẩy dữ liệu vào thành 1 màn hình duy nhất cho
//    cả "Ảnh" và "Video" (ghi đè trực tiếp kết quả AI lên khung), giữ nguyên "Camera".

(function () {
  'use strict';

  console.log('[Synexan UI Controller] Khởi động bộ điều khiển giao diện Single-Screen & FPS/Delay HUD...');

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
    // Chỉ giữ các frame trong vòng 1000ms gần nhất
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
        text.includes('Nguồn Đầu Vào') ||
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
  // 3. GỘP KHUNG HIỂN THỊ CHO CẢ "ẢNH" VÀ "VIDEO" (GIỮ NGUYÊN "CAMERA")
  // ----------------------------------------------------------------------------
  const TAB_CONFIGS = [
    {
      canvasId: 'tab5_calib_canvas',
      imageTabId: 'input_image',
      videoTabId: 'input_video',
      cameraTabId: 'input_camera'
    },
    {
      canvasId: 'traffic_canvas',
      imageTabId: 'traffic_input_image',
      videoTabId: 'traffic_input_video',
      cameraTabId: 'traffic_input_camera'
    },
    {
      canvasId: 'fusion_canvas',
      imageTabId: 'fusion_input_image',
      videoTabId: 'fusion_input_video',
      cameraTabId: 'fusion_input_camera'
    }
  ];

  function findRightColumn(canvasElem) {
    if (!canvasElem) return null;
    let col = canvasElem.closest('.column, .gr-column');
    if (col) return col;
    let p = canvasElem.parentElement;
    while (p && p !== document.body) {
      if (p.textContent && (p.textContent.includes('2. Màn Hình') || p.textContent.includes('Khung hình video'))) {
        return p;
      }
      p = p.parentElement;
    }
    return null;
  }

  function findLeftColumn(rightCol) {
    if (!rightCol || !rightCol.parentElement) return null;
    const row = rightCol.parentElement;
    for (let i = 0; i < row.children.length; i++) {
      const child = row.children[i];
      if (child !== rightCol) {
        return child;
      }
    }
    return null;
  }

  function getActiveSubMode(leftCol, cfg) {
    // Kiểm tra theo nút tab đang được chọn (Ảnh / Video / Camera)
    if (leftCol) {
      const selectedBtns = leftCol.querySelectorAll('button.selected, button[aria-selected="true"]');
      for (const btn of selectedBtns) {
        const t = (btn.textContent || '').trim().toLowerCase();
        if (t.includes('camera')) return 'camera';
        if (t.includes('video')) return 'video';
        if (t.includes('ảnh') || t.includes('anh')) return 'image';
      }
    }
    // Fallback kiểm tra phần tử DOM đang hiển thị
    const camEl = document.getElementById(cfg.cameraTabId);
    if (camEl && camEl.offsetParent !== null && getComputedStyle(camEl).display !== 'none') {
      return 'camera';
    }
    const imgEl = document.getElementById(cfg.imageTabId);
    if (imgEl && imgEl.offsetParent !== null && getComputedStyle(imgEl).display !== 'none') {
      return 'image';
    }
    return 'video';
  }

  function getCanvasImageSrc(canvasElem) {
    if (!canvasElem) return '';
    const img = canvasElem.querySelector('img');
    if (img) {
      const s = img.currentSrc || img.src || img.getAttribute('src') || '';
      if (s && s.length > 20) return s;
    }
    const c = canvasElem.querySelector('canvas');
    if (c) {
      try {
        return c.toDataURL('image/jpeg', 0.92);
      } catch (e) {}
    }
    return '';
  }

  function ensureOverlayOnContainer(mediaContainer, outputSrc) {
    if (!mediaContainer || !outputSrc) return;

    if (getComputedStyle(mediaContainer).position === 'static') {
      mediaContainer.style.position = 'relative';
    }

    // Đảm bảo các nút Xóa (Clear / Modify) của Gradio luôn nằm trên lớp phủ để người dùng đổi file dễ dàng
    const actionButtons = mediaContainer.querySelectorAll('button, .modify-upload, [aria-label="Clear"], [aria-label="Remove"]');
    actionButtons.forEach((btn) => {
      if (btn.parentElement && btn.parentElement !== mediaContainer) {
        btn.parentElement.style.zIndex = '40';
      }
      btn.style.zIndex = '40';
    });

    let overlayWrap = mediaContainer.querySelector('.synexan-single-screen-overlay-wrap');
    if (!overlayWrap) {
      overlayWrap = document.createElement('div');
      overlayWrap.className = 'synexan-single-screen-overlay-wrap';
      overlayWrap.style.cssText = `
        position: absolute;
        inset: 0;
        width: 100%;
        height: 100%;
        background: #0b0f19;
        border-radius: 8px;
        z-index: 15;
        pointer-events: none;
        display: flex;
        align-items: center;
        justify-content: center;
        overflow: hidden;
      `;

      const overlayImg = document.createElement('img');
      overlayImg.className = 'synexan-single-screen-overlay-img';
      overlayImg.style.cssText = `
        width: 100%;
        height: 100%;
        max-height: 100%;
        object-fit: contain;
        display: block;
        border-radius: 8px;
      `;
      overlayWrap.appendChild(overlayImg);

      // Gắn HUD FPS & Delay trực tiếp ở góc trái trên của khung hiển thị
      const hudBadge = document.createElement('div');
      hudBadge.className = 'synexan-hud-perf-badge';
      hudBadge.style.cssText = `
        position: absolute;
        top: 10px;
        left: 10px;
        background: rgba(15, 23, 42, 0.88);
        color: #38bdf8;
        border: 1px solid rgba(56, 189, 248, 0.4);
        font-size: 12px;
        font-weight: 700;
        padding: 4px 10px;
        border-radius: 6px;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45);
        z-index: 20;
        pointer-events: none;
        letter-spacing: 0.3px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      `;
      hudBadge.textContent = getPerfText();
      overlayWrap.appendChild(hudBadge);

      mediaContainer.appendChild(overlayWrap);
    }

    const overlayImg = overlayWrap.querySelector('.synexan-single-screen-overlay-img');
    if (overlayImg && overlayImg.getAttribute('data-last-src') !== outputSrc) {
      overlayImg.src = outputSrc;
      overlayImg.setAttribute('data-last-src', outputSrc);
    }
    overlayWrap.style.display = 'flex';
  }

  function removeOverlayFromContainer(container) {
    if (!container) return;
    const wraps = container.querySelectorAll('.synexan-single-screen-overlay-wrap');
    wraps.forEach((w) => {
      w.style.display = 'none';
    });
  }

  function syncTabSingleScreen(cfg) {
    const canvasElem = document.getElementById(cfg.canvasId);
    if (!canvasElem) return;

    const rightCol = findRightColumn(canvasElem);
    const leftCol = findLeftColumn(rightCol);
    if (!rightCol || !leftCol) return;

    const mode = getActiveSubMode(leftCol, cfg);
    const outputSrc = getCanvasImageSrc(canvasElem);

    // Kiểm tra xem canvas bên phải có vừa cập nhật frame mới không để tính FPS
    if (outputSrc && lastFrameSrcByCanvas[cfg.canvasId] !== outputSrc) {
      if (lastFrameSrcByCanvas[cfg.canvasId]) {
        recordNewFrameAndComputeFps();
      }
      lastFrameSrcByCanvas[cfg.canvasId] = outputSrc;
    }

    if (mode === 'camera') {
      // Chế độ Camera: Giữ nguyên 2 cột như hiện tại
      if (rightCol.__synexanHidden) {
        rightCol.style.position = '';
        rightCol.style.left = '';
        rightCol.style.top = '';
        rightCol.style.width = '';
        rightCol.style.height = '';
        rightCol.style.opacity = '';
        rightCol.style.pointerEvents = '';
        rightCol.style.overflow = '';
        rightCol.__synexanHidden = false;
      }
      leftCol.style.flex = '';
      leftCol.style.maxWidth = '';
      return;
    }

    // Chế độ Ảnh hoặc Video: Gộp thành 1 khung hiển thị duy nhất
    // Ẩn cột phải khỏi bố cục (nhưng vẫn giữ active trong DOM để Gradio render 30 FPS liên tục)
    rightCol.style.position = 'fixed';
    rightCol.style.left = '-10000px';
    rightCol.style.top = '0';
    rightCol.style.width = '10px';
    rightCol.style.height = '10px';
    rightCol.style.opacity = '0';
    rightCol.style.pointerEvents = 'none';
    rightCol.style.overflow = 'hidden';
    rightCol.__synexanHidden = true;

    leftCol.style.flex = '1 1 100%';
    leftCol.style.maxWidth = '100%';

    if (!outputSrc) return;

    if (mode === 'image') {
      const imgTab = document.getElementById(cfg.imageTabId);
      if (!imgTab) return;
      const uploadedImg = imgTab.querySelector('img');
      if (uploadedImg && uploadedImg.parentElement) {
        ensureOverlayOnContainer(uploadedImg.parentElement, outputSrc);
      } else {
        removeOverlayFromContainer(imgTab);
      }
    } else if (mode === 'video') {
      const vidTab = document.getElementById(cfg.videoTabId);
      if (!vidTab) return;
      const uploadedVid = vidTab.querySelector('video');
      if (uploadedVid && uploadedVid.parentElement) {
        // Tạm dừng phát thẻ <video> gốc khi lớp kết quả AI đang hiển thị đè lên
        if (!uploadedVid.paused) {
          try { uploadedVid.pause(); } catch (e) {}
        }
        ensureOverlayOnContainer(uploadedVid.parentElement, outputSrc);
      } else {
        removeOverlayFromContainer(vidTab);
      }
    }
  }

  function attachCanvasObservers() {
    TAB_CONFIGS.forEach((cfg) => {
      const canvasElem = document.getElementById(cfg.canvasId);
      if (canvasElem && !canvasElem.__synexanObserved) {
        canvasElem.__synexanObserved = true;
        const obs = new MutationObserver(() => {
          syncTabSingleScreen(cfg);
        });
        obs.observe(canvasElem, {
          childList: true,
          subtree: true,
          attributes: true,
          attributeFilter: ['src']
        });
      }
      syncTabSingleScreen(cfg);
    });
  }

  function masterTick() {
    sanitizeAllGradioEmojis();
    attachHeaderPerfBadges();
    attachCanvasObservers();
  }

  setInterval(masterTick, 250);
  window.addEventListener('DOMContentLoaded', masterTick);
  window.addEventListener('load', masterTick);
})();
