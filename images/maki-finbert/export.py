"""Build-time script: export ProsusAI/finbert to FP32 ONNX.

Uses the programmatic API instead of optimum-cli to avoid CLI
argument parsing issues across optimum versions.
"""
from optimum.exporters.onnx import main_export

main_export(
    model_name_or_path="ProsusAI/finbert",
    output="/tmp/finbert-fp32",
    task="text-classification",
)
