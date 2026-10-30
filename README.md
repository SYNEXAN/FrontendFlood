# 🌊 AI Urban Flood & Traffic Congestion Monitor (Official SYNEXAN)

> **Hệ thống Giám Sát Ngập Lụt & Ùn Tắc Đô Thị Thời Gian Thực**  
> Phân tách kiến trúc hiện đại: **Frontend trên Render.com** + **Backend AI trên Kaggle Cloud GPU (Tesla T4)**.

---

## 🏛️ Kiến Trúc Hệ Thống (Decoupled 3-Tier Architecture)

```
[ Khách Hàng (Mọi Thiết Bị) ]
               │
               ▼
[ Tầng 1: Frontend (Render.com) ]
  • Single Page Application (HTML5 + Tailwind CSS + Canvas Overlay)
  • Hoạt động 24/7 trên CDN toàn cầu của Render, không bao giờ bị ngủ đông
  • 3 Tab Chuyên Biệt: Flood (Ngập), Traffic (Xe), Dual Fusion (Cảnh Báo Kép)
               │
               ▼ (Đường truyền bảo mật Ngrok / Cloudflare Tunnel)
[ Tầng 2 & 3: Backend AI & Stream Engine (Kaggle Cloud GPU Tesla T4) ]
  • FastAPI High-Performance Server (`api_server.py`)
  • 2 Mô Hình AI Thật: YOLOv8s-seg (Phân đoạn ngập) + YOLOv8s (Phương tiện)
  • Staff Gauge Engine (Đo mực nước cm theo cọc tiêu / vật chuẩn)
  • Traffic Engine (ByteTrack + Phân loại xe máy, ô tô, tải, buýt + Chỉ số PCU)
  • MJPEG Streaming Server cho Camera CCTV trực tiếp (30 FPS)
```

---

## 📁 Cấu Trúc Thư Mục Dự Án (`FloodDemo`)

```
.
├── frontend/                          # Giao diện người dùng (Deploy lên Render.com)
│   ├── public/
│   │   ├── index.html                 # SPA thuần (3 Tabs, Canvas, KHÔNG DÙNG IFRAME)
│   │   ├── css/style.css              # Hiệu ứng radar, cảnh báo nhấp nháy
│   │   └── js/
│   │       ├── config.js              # Quản lý Backend URL động (localStorage)
│   │       ├── api_interceptor.js     # Bộ đón bắt thông minh (Auto-Wake Kaggle)
│   │       └── app.js                 # Điều khiển vẽ Canvas, âm thanh báo động
│   ├── server.js                      # Node.js Server phục vụ web & kích hoạt Kaggle
│   ├── package.json
│   └── render.yaml                    # File cấu hình 1-Click Deploy Render.com
│
├── backend/                           # Mã nguồn AI Backend (Chạy trên Kaggle GPU)
│   ├── api_server.py                  # FastAPI REST & Streaming Engine
│   ├── fixed_object_gauge.py          # Thuật toán đo mực nước cọc tiêu (cm)
│   ├── traffic_congestion_engine.py   # Thuật toán đếm xe & tính PCU
│   ├── adaptive_hybrid_depth_engine.py# Thuật toán đo sâu thích ứng dự phòng
│   └── requirements.txt               # Thư viện: fastapi, uvicorn, ultralytics...
│
├── notebooks/                         # Triển khai Kaggle GPU
│   ├── floodkaggleapi.ipynb           # Notebook 1-Click tải model, chạy FastAPI + Ngrok
│   └── kernel-metadata.json
│
├── .github/
│   └── workflows/
│       └── wake_kaggle.yml            # Webhook tự động đánh thức Kaggle an toàn
└── README.md
```

---

## 🚀 Hướng Dẫn Triển Khai (Deployment Guide)

### 1. Triển Khai Frontend Trên Render.com (Chỉ mất 2 phút)

1. Đăng nhập vào [Render Dashboard](https://dashboard.render.com).
2. Bấm **New +** -> Chọn **Web Service** (hoặc **Static Site**).
3. Kết nối với repository GitHub: `https://github.com/SYNEXAN/FrontendFlood`.
4. Điền các thông số:
   - **Name**: `flood-traffic-monitor` (hoặc tên tùy thích).
   - **Root Directory**: `frontend`.
   - **Build Command**: `npm install` (nếu chọn Web Service) hoặc để trống (nếu chọn Static Site).
   - **Start Command**: `node server.js` (nếu chọn Web Service) hoặc để trống.
   - **Publish Directory**: `public` (nếu chọn Static Site).
5. Bấm **Create Service** -> Render sẽ cấp đường link chính thức dạng:  
   👉 `https://flood-traffic-monitor.onrender.com`

---

### 2. Khởi Chạy Backend AI Trên Kaggle Cloud GPU

1. Mở Notebook trên Kaggle: [nguynvlun/floodkaggleapi](https://www.kaggle.com/code/nguynvlun/floodkaggleapi).
2. Đảm bảo cấu hình: **Accelerator = GPU T4 x 2** (hoặc GPU P100) và **Internet = ON**.
3. Bấm **Run All**.
4. Notebook sẽ:
   - Tự động đồng bộ mã nguồn backend mới nhất từ GitHub.
   - Tự động tải 2 bộ trọng số mô hình `vietnam_flood_yolov8s_seg.pt` và `yolov8s_vehicle.pt`.
   - Mở tunnel Ngrok tĩnh tại: `https://hush-harmless-erased.ngrok-free.dev`.
   - Chạy server FastAPI tại cổng 8000.

---

## 🔌 Đặc Tả API Cốt Lõi (API Endpoints)

| Phương Thức | Endpoint | Mô Tả |
| :---: | :--- | :--- |
| `GET` | `/api/v1/health` | Kiểm tra trạng thái máy chủ, tình trạng VRAM và GPU Tesla T4 |
| `POST` | `/api/v1/predict/image` | Nhận ảnh -> Trả về độ sâu ngập (cm), BBox xe, PCU, cảnh báo kép và ảnh vẽ sẵn |
| `GET` | `/api/v1/stream/live?source={rtsp_url}` | Cung cấp luồng video MJPEG Real-time 30 FPS từ camera giao thông |

---

## ⚡ Cơ Chế Tự Động Kích Hoạt Trong Suốt (Transparent Auto-Wake)

Khi khách hàng truy cập web trên Render:
- Nếu Backend Kaggle đang chạy: Phân tích hiển thị ngay tức thì (<0.1s).
- Nếu Backend Kaggle đang tắt: Trình duyệt tự hiện màn hình loading sang trọng, gửi tín hiệu ngầm để kích hoạt GPU trên Kaggle. Ngay khi GPU sẵn sàng, hệ thống **tự động phân tích ngay dữ liệu mà khách hàng vừa gửi** mà không cần thao tác thêm bất kỳ nút nào!

---
© 2026 **SYNEXAN AI LAB** - All Rights Reserved.
