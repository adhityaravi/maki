"""Build-time script: export ProsusAI/finbert to ONNX.

Uses the stable high-level ORT API (same class used at runtime)
instead of the internal optimum-cli / main_export path.
"""

from optimum.onnxruntime import ORTModelForSequenceClassification
from transformers import AutoTokenizer

print("Exporting ProsusAI/finbert to ONNX...")
model = ORTModelForSequenceClassification.from_pretrained(
    "ProsusAI/finbert",
    export=True,
)
model.save_pretrained("/tmp/finbert-fp32")

tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
tokenizer.save_pretrained("/tmp/finbert-fp32")
print("Done → /tmp/finbert-fp32")
