"""
AI Urban Flood & Traffic Monitor - High-Performance FastAPI Backend Engine
Designed for Kaggle Tesla T4 GPU & Seamless Client-Facing Real-time Streaming
"""

import io
import os
import base64
import cv2
import numpy as np
import torch
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from ultralytics import YOLO

# Import specialized engines
from fixed_object_gauge import measure_depth_via_fixed_object
from traffic_congestion_engine import TrafficCongestionEngine

app = FastAPI(
    title="AI Urban Flood & Traffic Monitor API",
    description="Decoupled AI Engine for Real-time Flood Depth Estimation and Traffic Congestion Analysis",
    version="2.0.0"
)

# Enable CORS for Render.com, GitHub Pages, localhost and all client origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"
print(f"[INFO] Initializing AI Models on {DEVICE}...")

# Model weights paths - check both current directory and models/ directory
def resolve_weight(name):
    candidates = [name, os.path.join("models", name), os.path.join("..", "models", name)]
    for c in candidates:
        if os.path.exists(c):
            return c
    return name

FLOOD_WEIGHTS = resolve_weight(os.environ.get("FLOOD_WEIGHTS", "vietnam_flood_yolov8s_seg.pt"))
VEHICLE_WEIGHTS = resolve_weight(os.environ.get("VEHICLE_WEIGHTS", "yolov8s_vehicle.pt"))

flood_model = None
vehicle_model = None
traffic_engine = None

def get_models():
    global flood_model, vehicle_model, traffic_engine
    if flood_model is None:
        print(f"[INFO] Loading Flood Model: {FLOOD_WEIGHTS}")
        flood_model = YOLO(FLOOD_WEIGHTS).to(DEVICE)
    if vehicle_model is None:
        print(f"[INFO] Loading Vehicle Model: {VEHICLE_WEIGHTS}")
        vehicle_model = YOLO(VEHICLE_WEIGHTS).to(DEVICE)
    if traffic_engine is None:
        traffic_engine = TrafficCongestionEngine()
    return flood_model, vehicle_model, traffic_engine

# Pre-load models on startup if weights exist
try:
    if os.path.exists(FLOOD_WEIGHTS) and os.path.exists(VEHICLE_WEIGHTS):
        get_models()
        print("[INFO] AI Models loaded successfully into VRAM.")
    else:
        print(f"[WARN] Model weights not found in current directory ({os.getcwd()}). Waiting for download...")
except Exception as e:
    print(f"[WARN] Could not pre-load models: {e}")

@app.get("/")
def root():
    return {
        "system": "AI Urban Flood & Traffic Monitor",
        "version": "2.0.0",
        "status": "online",
        "device": DEVICE,
        "docs_url": "/docs"
    }

@app.get("/api/v1/health")
def health_check():
    gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    vram_alloc = round(torch.cuda.memory_allocated(0) / 1024**2, 1) if torch.cuda.is_available() else 0
    return {
        "status": "ready",
        "device": DEVICE,
        "gpu_name": gpu_name,
        "vram_allocated_mb": vram_alloc,
        "models_ready": flood_model is not None and vehicle_model is not None
    }

@app.post("/api/v1/predict/image")
async def predict_image(
    file: UploadFile = File(...),
    gauge_type: str = Form("staff_gauge"),
    conf: float = Form(0.35)
):
    try:
        f_model, v_model, t_engine = get_models()
        contents = await file.read()
        nparr = np.frombuffer(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(status_code=400, detail="Invalid image file format")

        # 1. Inference Flood Segmentation
        flood_res = f_model(img, conf=conf, verbose=False)[0]
        has_flood = flood_res.masks is not None and len(flood_res.masks) > 0

        # 2. Inference Vehicle Detection
        vehicle_res = v_model(img, conf=conf, verbose=False)[0]

        # 3. Water Depth Measurement via Fixed Object Gauge
        depth_info = measure_depth_via_fixed_object(img, flood_res, vehicle_res, gauge_type=gauge_type)
        water_depth = float(depth_info.get("depth_cm", 0.0))

        # 4. Extract Vehicles Detections & PCU
        vehicles_data = []
        pcu_total = 0.0
        pcu_weights = {"motorcycle": 0.3, "car": 1.0, "truck": 2.0, "bus": 2.5}

        if vehicle_res.boxes is not None and len(vehicle_res.boxes) > 0:
            boxes = vehicle_res.boxes.xyxy.cpu().numpy()
            classes = vehicle_res.boxes.cls.cpu().numpy().astype(int)
            scores = vehicle_res.boxes.conf.cpu().numpy()

            for box, cls_idx, score in zip(boxes, classes, scores):
                cls_name = v_model.names.get(cls_idx, f"class_{cls_idx}")
                weight = pcu_weights.get(cls_name.lower(), 1.0)
                pcu_total += weight
                vehicles_data.append({
                    "bbox": [round(float(coord), 1) for coord in box],
                    "class": cls_name,
                    "confidence": round(float(score), 3),
                    "pcu": weight
                })

        # 5. Congestion & Dual Alarm Assessment
        if pcu_total > 8.0:
            congestion_level = "HEAVY"
        elif pcu_total > 3.0:
            congestion_level = "MODERATE"
        else:
            congestion_level = "LIGHT"

        is_dual_alarm = (water_depth >= 20.0) and (len(vehicles_data) >= 3 or pcu_total >= 3.0)
        severity = "DANGER" if water_depth > 30.0 else ("WARNING" if water_depth > 15.0 else "NORMAL")

        # 6. Render visual overlay (Mask + BBoxes)
        annotated = img.copy()
        if flood_res.masks:
            annotated = flood_res.plot(img=annotated)
        if vehicle_res.boxes:
            annotated = vehicle_res.plot(img=annotated)

        # Draw Water Depth Badge on top-left of image
        badge_text = f"WATER DEPTH: {water_depth:.1f} cm ({severity})"
        badge_color = (0, 0, 255) if severity == "DANGER" else ((0, 165, 255) if severity == "WARNING" else (0, 200, 0))
        cv2.putText(annotated, badge_text, (20, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 4)
        cv2.putText(annotated, badge_text, (20, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.9, badge_color, 2)

        # Draw Congestion Badge
        cong_text = f"VEHICLES: {len(vehicles_data)} | PCU: {pcu_total:.1f} ({congestion_level})"
        cv2.putText(annotated, cong_text, (20, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4)
        cv2.putText(annotated, cong_text, (20, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        if is_dual_alarm:
            alert_text = "DUAL ALARM: HIGH FLOOD & TRAFFIC CONGESTION"
            cv2.putText(annotated, alert_text, (20, 125), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4)
            cv2.putText(annotated, alert_text, (20, 125), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

        # Encode annotated image as Base64 JPEG
        _, buffer = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
        img_b64 = base64.b64encode(buffer).decode("utf-8")

        return {
            "status": "success",
            "flood": {
                "detected": has_flood,
                "depth_cm": round(water_depth, 1),
                "severity": severity,
                "gauge_used": gauge_type
            },
            "traffic": {
                "total_vehicles": len(vehicles_data),
                "pcu_total": round(pcu_total, 2),
                "congestion_level": congestion_level,
                "vehicles": vehicles_data
            },
            "dual_alarm": {
                "triggered": is_dual_alarm,
                "message": "🚨 CẢNH BÁO KÉP: ÙN TẮC DO NGẬP ÚNG SÂU" if is_dual_alarm else "Lưu thông bình thường"
            },
            "annotated_image": f"data:image/jpeg;base64,{img_b64}"
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/stream/live")
def stream_camera(source: str):
    """
    Real-Time Camera / RTSP / Video Stream Endpoint (MJPEG)
    Pulls directly on server GPU and yields annotated frames.
    """
    f_model, v_model, _ = get_models()

    def generate_frames():
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            print(f"[ERROR] Cannot open stream source: {source}")
            return

        frame_idx = 0
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            frame_idx += 1
            # Infer every frame or skip odd frames if source has high FPS
            f_res = f_model(frame, conf=0.35, verbose=False)[0]
            v_res = v_model(frame, conf=0.35, verbose=False)[0]

            out = frame.copy()
            if f_res.masks:
                out = f_res.plot(img=out)
            if v_res.boxes:
                out = v_res.plot(img=out)

            # Compact JPEG compression for smooth bandwidth streaming
            _, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 70])
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")

        cap.release()

    return StreamingResponse(
        generate_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
