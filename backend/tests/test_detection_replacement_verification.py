import pytest
from uuid import uuid4
from sqlalchemy import select, func
from unittest.mock import MagicMock

from app.core.database import AsyncSessionLocal
from app.models.session import SessionModel
from app.models.image import ImageModel, DetectedObjectModel
from app.repositories.image_repo import ImageRepository
from app.services.image_service import get_image_service
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.mark.asyncio
async def test_existing_image_record_is_preserved():
    """
    Criterion 1: Existing image record is preserved.
    Verifies that re-running detection does not alter or re-create the ImageModel row
    (id, filename, storage_path, width, height, session_id, and created_at remain identical).
    """
    async with AsyncSessionLocal() as db:
        session_id = uuid4()
        image_id = uuid4()

        # Seed session and image
        session = SessionModel(id=session_id, title="Surveillance Alpha")
        db.add(session)
        await db.commit()

        orig_image = await ImageRepository.create(
            db=db,
            image_id=image_id,
            session_id=session_id,
            filename="surveillance_target.jpg",
            storage_path="/tmp/surveillance_target.jpg",
            mime_type="image/jpeg",
            file_size_bytes=102400,
            width=1920,
            height=1080,
        )
        orig_created_at = orig_image.created_at

        # Initial detection
        initial_dets = [
            {"class_name": "car", "confidence": 0.85, "bbox": {"x_min": 10, "y_min": 10, "x_max": 50, "y_max": 50}}
        ]
        await ImageRepository.replace_detected_objects(db, image_id, initial_dets)

        # Re-run replacement with new detections
        new_dets = [
            {"class_name": "truck", "confidence": 0.95, "bbox": {"x_min": 100, "y_min": 100, "x_max": 300, "y_max": 300}},
            {"class_name": "person", "confidence": 0.91, "bbox": {"x_min": 40, "y_min": 50, "x_max": 80, "y_max": 160}},
        ]
        await ImageRepository.replace_detected_objects(db, image_id, new_dets)

        # Retrieve image from database
        persisted_image = await ImageRepository.get_by_id(db, image_id, load_objects=True)
        assert persisted_image is not None
        assert persisted_image.id == image_id
        assert persisted_image.session_id == session_id
        assert persisted_image.filename == "surveillance_target.jpg"
        assert persisted_image.storage_path == "/tmp/surveillance_target.jpg"
        assert persisted_image.width == 1920
        assert persisted_image.height == 1080
        assert persisted_image.file_size_bytes == 102400
        assert persisted_image.mime_type == "image/jpeg"
        assert persisted_image.created_at == orig_created_at


@pytest.mark.asyncio
async def test_previous_detections_are_updated_appropriately():
    """
    Criterion 2: Previous detections are updated appropriately.
    Verifies that the new detection set accurately reflects the updated classifications and confidences.
    """
    async with AsyncSessionLocal() as db:
        session_id = uuid4()
        image_id = uuid4()

        db.add(SessionModel(id=session_id, title="Test Session"))
        await db.commit()

        await ImageRepository.create(
            db, image_id, session_id, "recon.jpg", "/tmp/recon.jpg", "image/jpeg", 5000, 800, 600
        )

        # Initial detection
        await ImageRepository.replace_detected_objects(
            db,
            image_id,
            [{"class_name": "boat", "confidence": 0.50, "bbox": {"x_min": 0, "y_min": 0, "x_max": 10, "y_max": 10}}],
        )

        # Updated detection
        updated_detections = [
            {"class_name": "submarine", "confidence": 0.98, "bbox": {"x_min": 20, "y_min": 30, "x_max": 200, "y_max": 80}},
            {"class_name": "helicopter", "confidence": 0.89, "bbox": {"x_min": 50, "y_min": 10, "x_max": 90, "y_max": 40}},
        ]
        result = await ImageRepository.replace_detected_objects(db, image_id, updated_detections)

        assert len(result) == 2
        classes = {d.class_name for d in result}
        assert classes == {"submarine", "helicopter"}
        confidences = {d.class_name: d.confidence_score for d in result}
        assert confidences["submarine"] == 0.98
        assert confidences["helicopter"] == 0.89


@pytest.mark.asyncio
async def test_new_detections_are_saved_in_database():
    """
    Criterion 3: New detections are saved in the database.
    Verifies that the new detections are persisted to the database and can be queried directly via SQL.
    """
    async with AsyncSessionLocal() as db:
        session_id = uuid4()
        image_id = uuid4()

        db.add(SessionModel(id=session_id, title="Test Session"))
        await db.commit()

        await ImageRepository.create(
            db, image_id, session_id, "recon.jpg", "/tmp/recon.jpg", "image/jpeg", 5000, 800, 600
        )

        target_obj_id = uuid4()
        new_dets = [
            {
                "id": str(target_obj_id),
                "class_name": "armored_vehicle",
                "confidence": 0.965,
                "bbox": {"x_min": 12.5, "y_min": 34.0, "x_max": 156.5, "y_max": 178.0},
            }
        ]
        await ImageRepository.replace_detected_objects(db, image_id, new_dets)

        # Direct SQL query against detected_objects table
        stmt = select(DetectedObjectModel).where(DetectedObjectModel.image_id == image_id)
        rows = (await db.execute(stmt)).scalars().all()

        assert len(rows) == 1
        saved = rows[0]
        assert saved.id == target_obj_id
        assert saved.class_name == "armored_vehicle"
        assert saved.confidence_score == 0.965
        assert saved.bbox_x_min == 12.5
        assert saved.bbox_y_min == 34.0
        assert saved.bbox_x_max == 156.5
        assert saved.bbox_y_max == 178.0


@pytest.mark.asyncio
async def test_old_detections_dont_remain_incorrectly_mixed_with_new_detections():
    """
    Criterion 4: Old detections don't remain incorrectly mixed with new detections.
    Verifies that previous detection rows are deleted and do not contaminate new detection queries.
    """
    async with AsyncSessionLocal() as db:
        session_id = uuid4()
        image_id = uuid4()

        db.add(SessionModel(id=session_id, title="Test Session"))
        await db.commit()

        await ImageRepository.create(
            db, image_id, session_id, "recon.jpg", "/tmp/recon.jpg", "image/jpeg", 5000, 800, 600
        )

        # Seed 3 old detections: car, bus, traffic light
        old_dets = [
            {"class_name": "car", "confidence": 0.70, "bbox": {"x_min": 1, "y_min": 1, "x_max": 10, "y_max": 10}},
            {"class_name": "bus", "confidence": 0.75, "bbox": {"x_min": 2, "y_min": 2, "x_max": 20, "y_max": 20}},
            {"class_name": "traffic light", "confidence": 0.80, "bbox": {"x_min": 3, "y_min": 3, "x_max": 30, "y_max": 30}},
        ]
        await ImageRepository.replace_detected_objects(db, image_id, old_dets)

        # Verify old detections exist
        stmt = select(DetectedObjectModel).where(DetectedObjectModel.image_id == image_id)
        old_rows = (await db.execute(stmt)).scalars().all()
        assert len(old_rows) == 3

        # Replace with 1 new detection: bicycle
        new_dets = [
            {"class_name": "bicycle", "confidence": 0.92, "bbox": {"x_min": 50, "y_min": 50, "x_max": 100, "y_max": 100}}
        ]
        await ImageRepository.replace_detected_objects(db, image_id, new_dets)

        # Verify only the new detection exists; none of the old ones remain
        new_rows = (await db.execute(stmt)).scalars().all()
        assert len(new_rows) == 1
        assert new_rows[0].class_name == "bicycle"

        classes_in_db = {r.class_name for r in new_rows}
        assert "car" not in classes_in_db
        assert "bus" not in classes_in_db
        assert "traffic light" not in classes_in_db


@pytest.mark.asyncio
async def test_repeated_reruns_dont_create_duplicate_detection_records():
    """
    Criterion 5: Repeated reruns don't create duplicate detection records.
    Verifies that calling replace_detected_objects multiple times in succession
    keeps only the latest detections and never accumulates duplicate records.
    """
    async with AsyncSessionLocal() as db:
        session_id = uuid4()
        image_id = uuid4()

        db.add(SessionModel(id=session_id, title="Test Session"))
        await db.commit()

        await ImageRepository.create(
            db, image_id, session_id, "recon.jpg", "/tmp/recon.jpg", "image/jpeg", 5000, 800, 600
        )

        stmt = select(func.count()).select_from(DetectedObjectModel).where(DetectedObjectModel.image_id == image_id)

        # Iteration 1: 3 detections
        iter1 = [
            {"class_name": "dog", "confidence": 0.8, "bbox": {"x_min": 1, "y_min": 1, "x_max": 10, "y_max": 10}},
            {"class_name": "cat", "confidence": 0.8, "bbox": {"x_min": 2, "y_min": 2, "x_max": 20, "y_max": 20}},
            {"class_name": "horse", "confidence": 0.8, "bbox": {"x_min": 3, "y_min": 3, "x_max": 30, "y_max": 30}},
        ]
        await ImageRepository.replace_detected_objects(db, image_id, iter1)
        count1 = (await db.execute(stmt)).scalar()
        assert count1 == 3

        # Iteration 2: Same 3 detections re-run
        await ImageRepository.replace_detected_objects(db, image_id, iter1)
        count2 = (await db.execute(stmt)).scalar()
        assert count2 == 3, f"Expected 3 records, but found {count2} (duplicate accumulation)"

        # Iteration 3: 2 different detections
        iter3 = [
            {"class_name": "airplane", "confidence": 0.95, "bbox": {"x_min": 5, "y_min": 5, "x_max": 50, "y_max": 50}},
            {"class_name": "drone", "confidence": 0.88, "bbox": {"x_min": 6, "y_min": 6, "x_max": 60, "y_max": 60}},
        ]
        await ImageRepository.replace_detected_objects(db, image_id, iter3)
        count3 = (await db.execute(stmt)).scalar()
        assert count3 == 2

        # Iteration 4: 0 detections (empty frame)
        await ImageRepository.replace_detected_objects(db, image_id, [])
        count4 = (await db.execute(stmt)).scalar()
        assert count4 == 0

        # Iteration 5: 1 detection
        iter5 = [{"class_name": "truck", "confidence": 0.91, "bbox": {"x_min": 0, "y_min": 0, "x_max": 100, "y_max": 100}}]
        await ImageRepository.replace_detected_objects(db, image_id, iter5)
        count5 = (await db.execute(stmt)).scalar()
        assert count5 == 1


@pytest.mark.asyncio
async def test_failed_detection_does_not_leave_partially_updated_database_records():
    """
    Criterion 6: A failed detection doesn't leave partially updated database records.
    Verifies that if an error occurs during replacement, the transaction rolls back cleanly:
    original records are preserved and no partial/corrupted records remain.
    """
    async with AsyncSessionLocal() as db:
        session_id = uuid4()
        image_id = uuid4()

        db.add(SessionModel(id=session_id, title="Test Session"))
        await db.commit()

        await ImageRepository.create(
            db, image_id, session_id, "recon.jpg", "/tmp/recon.jpg", "image/jpeg", 5000, 800, 600
        )

        # Seed valid initial detection: "tank"
        initial_dets = [
            {"class_name": "tank", "confidence": 0.99, "bbox": {"x_min": 10, "y_min": 20, "x_max": 100, "y_max": 120}}
        ]
        await ImageRepository.replace_detected_objects(db, image_id, initial_dets)

        # Verify initial detection is present
        stmt = select(DetectedObjectModel).where(DetectedObjectModel.image_id == image_id)
        seeded = (await db.execute(stmt)).scalars().all()
        assert len(seeded) == 1
        assert seeded[0].class_name == "tank"

        # Attempt replacement with invalid data that triggers an exception during _build_detected_objects
        # (e.g. invalid UUID format in 'id')
        corrupt_dets = [
            {"id": "NOT-A-VALID-HEX-UUID", "class_name": "corrupt_car", "confidence": 0.5, "bbox": {}}
        ]

        with pytest.raises(Exception):
            await ImageRepository.replace_detected_objects(db, image_id, corrupt_dets)

        # Re-query: The transaction rolled back, so "tank" must still be present and intact!
        after_failure = (await db.execute(stmt)).scalars().all()
        assert len(after_failure) == 1
        assert after_failure[0].class_name == "tank"
        assert after_failure[0].confidence_score == 0.99


@pytest.mark.asyncio
async def test_end_to_end_detect_api_replacement_guarantees(monkeypatch):
    """
    Integration test via HTTP API verifying all 6 guarantees end-to-end:
    - Image record preservation
    - Clean replacement on re-detection
    - No duplicate records across multiple slider adjustments
    - Rollback safety on failure
    """
    from PIL import Image
    import io

    # 1. Setup mock YOLO detections
    curr_dets = [
        {
            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            "class_id": 1,
            "class_name": "radar_station",
            "confidence": 0.95,
            "confidence_percentage": 95.0,
            "spatial_sector": "center",
            "area_ratio": 0.15,
            "bbox": {"x_min": 50.0, "y_min": 50.0, "x_max": 200.0, "y_max": 200.0},
            "normalized_bbox": {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4, "y_max": 0.4},
        }
    ]

    async def mock_detect(*args, **kwargs):
        return {
            "detections": curr_dets,
            "count": len(curr_dets),
            "image_dimensions": {"width": 640, "height": 480},
            "latency_ms": 2.5,
            "device": "cpu",
            "model": "yolov8n.pt",
        }

    monkeypatch.setattr("app.clients.yolo_client.YOLOClient.detect_objects_async", mock_detect)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create session
        s_res = await client.post("/api/v1/sessions", json={"title": "Radar Ops"})
        assert s_res.status_code == 201
        session_id = s_res.json()["id"]

        # Upload image
        buf = io.BytesIO()
        Image.new("RGB", (640, 480), color="blue").save(buf, format="JPEG")
        files = {"file": ("radar.jpg", buf.getvalue(), "image/jpeg")}
        up_res = await client.post("/api/v1/images/upload", files=files, data={"session_id": session_id})
        assert up_res.status_code == 201
        image_id = up_res.json()["id"]

        # Initial check: 1 detection (radar_station)
        assert len(up_res.json()["detected_objects"]) == 1
        assert up_res.json()["detected_objects"][0]["class_name"] == "radar_station"

        # Rerun 1: Detector returns 2 objects (missile_launcher, support_vehicle)
        curr_dets = [
            {
                "id": "e2e00000-0000-0000-0000-000000000001",
                "class_id": 2,
                "class_name": "missile_launcher",
                "confidence": 0.88,
                "confidence_percentage": 88.0,
                "spatial_sector": "top-left",
                "area_ratio": 0.1,
                "bbox": {"x_min": 10.0, "y_min": 10.0, "x_max": 80.0, "y_max": 80.0},
                "normalized_bbox": {"x_min": 0.02, "y_min": 0.02, "x_max": 0.15, "y_max": 0.15},
            },
            {
                "id": "e2e00000-0000-0000-0000-000000000002",
                "class_id": 3,
                "class_name": "support_vehicle",
                "confidence": 0.82,
                "confidence_percentage": 82.0,
                "spatial_sector": "bottom-right",
                "area_ratio": 0.12,
                "bbox": {"x_min": 200.0, "y_min": 200.0, "x_max": 350.0, "y_max": 350.0},
                "normalized_bbox": {"x_min": 0.35, "y_min": 0.35, "x_max": 0.65, "y_max": 0.65},
            },
        ]
        rerun1_res = await client.post(f"/api/v1/images/{image_id}/detect?confidence_threshold=0.50")
        assert rerun1_res.status_code == 200
        rerun1_data = rerun1_res.json()

        # Guarantee 1 & 2: Image record preserved, detections updated
        assert rerun1_data["id"] == image_id
        assert rerun1_data["filename"] == "radar.jpg"
        assert len(rerun1_data["detected_objects"]) == 2
        classes_rerun1 = {d["class_name"] for d in rerun1_data["detected_objects"]}
        assert classes_rerun1 == {"missile_launcher", "support_vehicle"}
        # Guarantee 4: Old detection "radar_station" is gone, not mixed in
        assert "radar_station" not in classes_rerun1

        # Rerun 2 (Repeated rerun): Verify no duplicates accumulate
        rerun2_res = await client.post(f"/api/v1/images/{image_id}/detect?confidence_threshold=0.50")
        assert rerun2_res.status_code == 200
        assert len(rerun2_res.json()["detected_objects"]) == 2

        # Check full session detail from DB
        sess_detail = (await client.get(f"/api/v1/sessions/{session_id}")).json()
        assert len(sess_detail["images"]) == 1
        assert len(sess_detail["images"][0]["detected_objects"]) == 2

        # Guarantee 6: Simulate failure during detection
        async def mock_fail(*args, **kwargs):
            raise RuntimeError("YOLO GPU hardware failure simulation")

        monkeypatch.setattr("app.clients.yolo_client.YOLOClient.detect_objects_async", mock_fail)
        failed_res = await client.post(f"/api/v1/images/{image_id}/detect?confidence_threshold=0.50")
        assert failed_res.status_code == 500

        # After failure, verify DB is NOT corrupted or emptied - the 2 objects are still there!
        sess_after = (await client.get(f"/api/v1/sessions/{session_id}")).json()
        assert len(sess_after["images"]) == 1
        assert len(sess_after["images"][0]["detected_objects"]) == 2
        classes_after = {d["class_name"] for d in sess_after["images"][0]["detected_objects"]}
        assert classes_after == {"missile_launcher", "support_vehicle"}
