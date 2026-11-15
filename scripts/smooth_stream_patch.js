// ==============================================================================
// SYNEXAN ULTRA-SMOOTH JITTER BUFFER VIDEO RENDERER (GPU ACCELERATED CANVAS)
// ==============================================================================
// Tối ưu hóa hiển thị luồng video AI trên Gradio bằng HTML5 Canvas và Hardware GPU.
// Sử dụng hàng đợi FIFO (Ring Buffer) 4-6 frames để triệt tiêu biến thiên mạng (jitter),
// kết hợp requestAnimationFrame giữ nhịp 30 FPS đều đặn và gửi telemetry về Parent Window.

(function() {
  'use strict';

  console.log('🚀 [Synexan Smooth Stream] Khởi động Bộ đệm Jitter Buffer...');

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

            // Vẽ HUD Badge: FPS & Độ trễ trực tiếp trên góc trên bên phải của video
            const fpsVal = this.currentFps > 0 ? this.currentFps : 30;
            const latVal = Math.round(this.queue.length * this.frameIntervalMs + 65);
            const hudText = `⚡ ${fpsVal} FPS • ${latVal}ms`;
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
      }

      requestAnimationFrame(this.renderLoop.bind(this));
    }

    emitTelemetry(mode = 'active') {
      try {
        const fpsVal = (mode === 'active' && this.currentFps > 0) ? this.currentFps : 0;
        const latVal = Math.round(this.queue.length * this.frameIntervalMs + 65);
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
    // Tìm các thẻ img của Gradio có chứa class hoặc thuộc tính hiển thị kết quả
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
