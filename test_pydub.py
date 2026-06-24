# test_pydub.py - run this in your venv
import sys
print(sys.executable)  # Confirm correct venv

try:
    from pydub import AudioSegment
    print("✅ pydub imported OK")
    
    # Test ffmpeg is found
    from pydub.utils import which
    ffmpeg = which("ffmpeg")
    print(f"ffmpeg path: {ffmpeg}")  # None = NOT FOUND = your real problem
    
except ImportError as e:
    print(f"❌ Import failed: {e}")
except Exception as e:
    print(f"❌ Other error: {e}")