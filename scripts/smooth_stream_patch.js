// ==============================================================================
// SYNEXAN ULTRA-SMOOTH JITTER BUFFER VIDEO RENDERER (GPU ACCELERATED CANVAS)
// ==============================================================================
// Tối ưu hóa hiển thị luồng video AI trên Gradio bằng HTML5 Canvas và Hardware GPU.
// Tự động nhúng badge hiển thị FPS & Độ trễ vào ngay thanh tiêu đề "Màn Hình Video",
// loại bỏ hoàn toàn các icon/emoji để giao diện đạt chuẩn chuyên nghiệp và thanh lịch.

(function() {
  'use strict';

  console.log('🚀 [Synexan Smooth Stream] Khởi động Bộ đệm Jitter Buffer...');

  let globalLatestFps = 0;
  let globalLatestLatency = 71;

  function updateHeaderBadges(fpsVal, latencyMs) {
    if (typeof latencyMs === 'number' && latencyMs > 0) globalLatestLatency = latencyMs;
    if (typeof fpsVal === 'number') globalLatestFps = fpsVal;

    const text = globalLatestFps > 0 
      ? `${globalLatestFps} FPS | Độ trễ: ${globalLatestLatency}ms`
      : `Standby | Độ trễ: ${globalLatestLatency}ms`;

    const badges = document.querySelectorAll('.synexan-header-perf-badge');
    badges.forEach((b) => {
      b.textContent = text;
      b.style.color = globalLatestFps >= 25 ? '#34d399' : (globalLatestFps > 0 ? '#38bdf8' : '#94a3b8');
      b.style.borderColor = globalLatestFps > 0 ? 'rgba(56, 189, 248, 0.4)' : 'rgba(148, 163, 184, 0.3)';
    });
  }

  function attachHeaderPerfBadges() {
    // Quét tìm các phần tử tiêu đề chứa "Màn Hình Video" hoặc "Màn Hình Giám Sát"
    const headings = document.querySelectorAll('h1, h2, h3, h4, h5, h6, .prose h4, .prose *');
    headings.forEach((el) => {
      const text = el.textContent || '';
      if (text.includes('Màn Hình Video') || text.includes('Màn Hình Giám Sát')) {
        // 1. Loại bỏ các emoji/icons (như 🖼️, ⚡) trong tiêu đề để giao diện sạch sẽ
        if (el.innerHTML.includes('🖼️') || el.innerHTML.includes('⚡')) {
          el.innerHTML = el.innerHTML.replace(/[🖼️⚡]\s*/g, '');
        }

        // 2. Chuyển tiêu đề thành flexbox để badge dạt sang góc phải ngay ngắn
        el.style.display = 'flex';
        el.style.justifyContent = 'space-between';
        el.style.alignItems = 'center';
        el.style.width = '100%';
        el.style.flexWrap = 'wrap';
        el.style.gap = '8px';

        // 3. Nếu chưa gắn badge, tạo mới và gắn vào góc phải
        if (!el.querySelector('.synexan-header-perf-badge')) {
          const badge = document.createElement('span');
          badge.className = 'synexan-header-perf-badge';
          badge.style.cssText = `
            font-size: 12px;
            font-weight: 600;
            color: #94a3b8;
            background: rgba(15, 23, 42, 0.75);
            border: 1px solid rgba(148, 163, 184, 0.3);
            border-radius: 6px;
            padding: 3px 10px;
            letter-spacing: 0.3px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.25);
            user-select: none;
            white-space: nowrap;
          `;
          const initText = globalLatestFps > 0 
            ? `${globalLatestFps} FPS | Độ trễ: ${globalLatestLatency}ms`
            : `Standby | Độ trễ: ${globalLatestLatency}ms`;
          badge.textContent = initText;
          el.appendChild(badge);
        }
      }
    });
  }

  // Quét định kỳ mỗi 800ms để gắn badge khi người dùng chuyển qua lại các Tab
  setInterval(attachHeaderPerfBadges, 800);
  window.addEventListener('DOMContentLoaded', attachHeaderPerfBadges);

  // Lắng nghe cập nhật ping khứ hồi từ Parent Window
  window.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'PING_UPDATE') {
      const pingVal = event.data.latencyMs;
      if (typeof pingVal === 'number' && pingVal > 0) {
        updateHeaderBadges(globalLatestFps, pingVal);
      }
    }
  });

  class SynexanJitterBuffer {
    constructor(targetFps = 30, targetBuffer = 5) {
      this.targetFps = targetFps;
      this.frameIntervalMs = 1000 / targetFps; // ~33.3ms
      this.targetBuffer = targetBuffer;
      this.queue = [];
      this.isPlaying = false;
      this.lastRenderTime = 0;
      this.fpsCounter = 0;
      this.fpsTimer = performance.now();
      this.currentFps = 0;
      this.canvas = null;
      this.ctx = null;
      this.activeImgElem = null;
      this.lastReceivedSrc = null;
      this.idleTimeout = null;
    }

    attachToElement(imgElem) {
      if (!imgElem || imgElem.__synexanAttached) return;
      imgElem.__synexanAttached = true;
      this.activeImgElem = imgElem;

      // 1. Tạo Canvas thay thế
      this.canvas = document.createElement('canvas');
      this.canvas.className = 'synexan-smooth-canvas';
      this.canvas.style.cssText = `
        width: 100%;
        height: 100%;
        max-height: 100%;
        object-fit: contain;
        display: block;
        border-radius: 8px;
        background: #0b0f19;
      `;
      this.ctx = this.canvas.getContext('2d', { alpha: false, desynchronized: true });

      // 2. Chèn canvas vào cùng container
      const parent = imgElem.parentNode;
      if (parent) {
        parent.style.position = 'relative';
        parent.insertBefore(this.canvas, imgElem);
        imgElem.style.display = 'none';
      }

      // 3. Lắng nghe thay đổi của thuộc tính src (Producer)
      const handleNewSrc = (src) => {
        if (!src || src === this.lastReceivedSrc) return;
        if (src.startsWith('data:image/') || src.startsWith('blob:') || src.startsWith('http')) {
          this.lastReceivedSrc = src;
          this.pushFrame(src);
        }
      };

      // Bắt giá trị src hiện tại nếu có
      if (imgElem.src) {
        handleNewSrc(imgElem.src);
      }

      const observer = new MutationObserver((mutations) => {
        for (const m of mutations) {
          if (m.type === 'attributes' && m.attributeName === 'src') {
            const newSrc = imgElem.getAttribute('src') || imgElem.src;
            handleNewSrc(newSrc);
          }
        }
      });
      observer.observe(imgElem, { attributes: true, attributeFilter: ['src'] });

      console.log('✅ [Synexan Smooth Stream] Đã gắn GPU Canvas Jitter Buffer vào phần tử ảnh!');
    }

    pushFrame(src) {
      const img = new Image();
      img.src = src;
      this.queue.push(img);

      // Reset timer chờ nhàn rỗi (idle reset sau 2.5s không có frame mới)
      clearTimeout(this.idleTimeout);
      this.idleTimeout = setTimeout(() => {
        if (this.queue.length === 0) {
          this.isPlaying = false;
          this.emitTelemetry('idle');
          updateHeaderBadges(0, globalLatestLatency);
        }
      }, 2500);

      // Kích hoạt phát khi gom đủ số frame pre-roll (lót đường 4-5 frames)
      if (!this.isPlaying && this.queue.length >= Math.min(3, this.targetBuffer)) {
        this.isPlaying = true;
        this.lastRenderTime = performance.now();
        requestAnimationFrame(this.renderLoop.bind(this));
      }
    }

    renderLoop(now) {
      if (!this.isPlaying) return;

      const delta = now - this.lastRenderTime;

      // Điều tốc thích ứng (Adaptive Pacing)
      let dynamicInterval = this.frameIntervalMs;
      if (this.queue.length > this.targetBuffer * 2) {
        dynamicInterval *= 0.90; // Buffer bị dồn ứ: tăng tốc 10% để xả
      } else if (this.queue.length <= 2) {
        dynamicInterval *= 1.10; // Buffer cạn dần: chậm lại 10% để giữ hình liên tục
      }

      if (delta >= dynamicInterval) {
        if (this.queue.length > 0) {
          const frame = this.queue.shift();
          if (frame.complete && frame.naturalWidth > 0) {
            if (this.canvas.width !== frame.naturalWidth || this.canvas.height !== frame.naturalHeight) {
              this.canvas.width = frame.naturalWidth;
              this.canvas.height = frame.naturalHeight;
            }
            this.ctx.drawImage(frame, 0, 0);
            this.fpsCounter++;

            // Vẽ HUD Badge: FPS & Độ trễ trực tiếp trên góc trên bên phải của video (Không icon)
            const fpsVal = this.currentFps > 0 ? this.currentFps : 30;
            const latVal = Math.round(this.queue.length * this.frameIntervalMs + globalLatestLatency);
            const hudText = `${fpsVal} FPS | Độ trễ: ${latVal}ms`;
            this.ctx.save();
            this.ctx.font = 'bold 12px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
            const textWidth = this.ctx.measureText(hudText).width;
            const boxX = this.canvas.width - textWidth - 20;
            const boxY = 10;
            this.ctx.fillStyle = 'rgba(15, 23, 42, 0.85)';
            if (typeof this.ctx.roundRect === 'function') {
              this.ctx.beginPath();
              this.ctx.roundRect(boxX, boxY, textWidth + 14, 24, 6);
              this.ctx.fill();
              this.ctx.strokeStyle = 'rgba(56, 189, 248, 0.5)';
              this.ctx.lineWidth = 1;
              this.ctx.stroke();
            } else {
              this.ctx.fillRect(boxX, boxY, textWidth + 14, 24);
            }
            this.ctx.fillStyle = '#38bdf8';
            this.ctx.fillText(hudText, boxX + 7, boxY + 16);
            this.ctx.restore();
          }
        }
        this.lastRenderTime = now;
      }

      // Đo FPS thực tế mỗi giây và gửi báo cáo về trang mẹ
      if (now - this.fpsTimer >= 1000) {
        this.currentFps = this.fpsCounter;
        this.fpsCounter = 0;
        this.fpsTimer = now;
        this.emitTelemetry('active');
        updateHeaderBadges(this.currentFps, globalLatestLatency);
      }

      requestAnimationFrame(this.renderLoop.bind(this));
    }

    emitTelemetry(mode = 'active') {
      try {
        const fpsVal = (mode === 'active' && this.currentFps > 0) ? this.currentFps : 0;
        const latVal = Math.round(this.queue.length * this.frameIntervalMs + globalLatestLatency);
        const payload = {
          type: 'STREAM_HEALTH',
          fps: fpsVal,
          bufferCount: this.queue.length,
          targetBuffer: this.targetBuffer,
          status: (mode === 'idle' || fpsVal === 0) ? 'idle' : (this.queue.length >= 2 ? 'healthy' : 'buffering'),
          latencyMs: latVal
        };
        window.parent.postMessage(payload, '*');
      } catch (e) {}
    }
  }

  // Tự động tìm kiếm các phần tử hình ảnh output trong Gradio và kích hoạt Jitter Buffer
  function autoAttachJitterBuffer() {
    const candidateImages = document.querySelectorAll('img[data-testid="detailed-image"], .image-container img, .output-image img, div[data-testid="image"] img');
    candidateImages.forEach((img) => {
      if (!img.__synexanAttached) {
        const renderer = new SynexanJitterBuffer(30, 5);
        renderer.attachToElement(img);
      }
    });
  }

  // Quét liên tục khi Gradio render các tab hoặc component động
  setInterval(autoAttachJitterBuffer, 1000);
  window.addEventListener('DOMContentLoaded', autoAttachJitterBuffer);
})();
