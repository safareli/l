import os
import sys

# Set OMP_NUM_THREADS before any ONNX/numpy imports.
# ONNX Runtime's OpenMP backend initializes at import time and defaults
# to using all CPU cores. On ARM (unknown CPU vendor to onnxruntime),
# intra_op_num_threads alone doesn't limit OpenMP parallelism.
# Previously torch.set_num_threads() did this implicitly via omp_set_num_threads().
if "OMP_NUM_THREADS" not in os.environ:
    # Parse --threads from argv before full argument parsing
    threads = 4  # default
    for i, arg in enumerate(sys.argv):
        if arg == "--threads" and i + 1 < len(sys.argv):
            try:
                threads = int(sys.argv[i + 1])
            except ValueError:
                pass
    os.environ["OMP_NUM_THREADS"] = str(threads)

from stt_streaming import main

main()
