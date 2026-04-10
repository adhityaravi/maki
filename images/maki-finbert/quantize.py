"""Build-time script: quantize exported FP32 FinBERT ONNX model to INT8.

Reads from /tmp/finbert-fp32 (written by optimum-cli export onnx),
writes INT8 model + all tokenizer artefacts to /app/model.
"""

import os
import shutil

from optimum.onnxruntime import ORTQuantizer
from optimum.onnxruntime.configuration import AutoQuantizationConfig

SRC = "/tmp/finbert-fp32"
DST = "/app/model"

os.makedirs(DST, exist_ok=True)

print(f"Quantizing {SRC} → {DST} (INT8 dynamic, AVX2)")
quantizer = ORTQuantizer.from_pretrained(SRC)
qconfig = AutoQuantizationConfig.avx2(is_static=False, per_channel=False)
quantizer.quantize(save_dir=DST, quantization_config=qconfig)

# ORTQuantizer outputs model_quantized.onnx; rename to model.onnx so the
# default ORTModelForSequenceClassification.from_pretrained() finds it.
quantized = os.path.join(DST, "model_quantized.onnx")
target = os.path.join(DST, "model.onnx")
if os.path.exists(quantized):
    os.rename(quantized, target)

# Copy any tokenizer / config artefacts not already written by the quantizer.
for fname in os.listdir(SRC):
    dst_path = os.path.join(DST, fname)
    if not os.path.exists(dst_path):
        shutil.copy(os.path.join(SRC, fname), dst_path)

model_mb = os.path.getsize(target) / 1e6
print(f"Done — model.onnx: {model_mb:.1f} MB")
