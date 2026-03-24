import os
import tempfile
from flask import Flask, request, jsonify
from nudenet import NudeDetector

app = Flask(__name__)

NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.50"))

detector = NudeDetector()

NSFW_LABELS = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED"
}


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "ok": True,
        "service": "nsfw-api"
    })


@app.route("/analyze", methods=["POST"])
def analyze():
    if "file" not in request.files:
        return jsonify({"error": "file missing"}), 400

    uploaded = request.files["file"]

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        uploaded.save(tmp.name)
        temp_path = tmp.name

    try:
        detections = detector.detect(temp_path)

        max_score = 0.0
        is_nsfw = False

        for item in detections:
            label = item.get("class", "")
            score = float(item.get("score", 0.0))

            if label in NSFW_LABELS and score > max_score:
                max_score = score

            if label in NSFW_LABELS and score >= NSFW_THRESHOLD:
                is_nsfw = True

        return jsonify({
            "is_nsfw": is_nsfw,
            "nsfw_score": max_score,
            "detections": detections
        })

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500

    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
