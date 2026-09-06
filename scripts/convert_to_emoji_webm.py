"""
scripts/convert_to_emoji_webm.py
==============================================================================
Script otomatisasi untuk mengonversi gambar/animasi/video (GIF, MP4, WebP, PNG)
menjadi format .webm resmi Telegram Animated Custom Emoji:
- Resolusi: Tepat 100 x 100 px
- Codec: VP9 (dengan dukungan transparansi alpha)
- Durasi: Maks 3.0 detik (looping)
- Ukuran: Maksimal 256 KB, tanpa audio
==============================================================================
Cara Pakai:
1. Letakkan file GIF / Video / Gambar ke folder `emoji_sources/`
2. Jalankan: python scripts/convert_to_emoji_webm.py
3. File hasil konversi (.webm) siap diunggah ke @Stickers ada di folder `emoji_output/`
==============================================================================
"""

import os
import sys
import subprocess
import shutil

# Set stdout encoding to utf-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import imageio_ffmpeg
    FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_EXE = shutil.which("ffmpeg")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_DIR = os.path.join(PROJECT_ROOT, "emoji_sources")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "emoji_output")


def convert_file_to_telegram_webm(input_path: str, output_path: str) -> bool:
    """Mengonversi single media file ke 100x100 VP9 WebM via FFmpeg."""
    if not FFMPEG_EXE:
        print("❌ FFmpeg executable tidak ditemukan. Jalankan: pip install imageio[ffmpeg]")
        return False

    # Filter FFmpeg: Scale ke 100x100 dengan padding transparan (preserve aspect ratio)
    vf_filter = (
        "scale=100:100:force_original_aspect_ratio=decrease,"
        "pad=100:100:(100-iw)/2:(100-ih)/2:color=0x00000000,"
        "fps=30"
    )

    cmd = [
        FFMPEG_EXE,
        "-y",               # Overwrite jika sudah ada
        "-i", input_path,   # File input
        "-t", "3.0",        # Maksimal 3 detik
        "-vf", vf_filter,   # Resize 100x100 & 30fps
        "-c:v", "libvpx-vp9",
        "-b:v", "180k",     # Target bitrate agar < 256 KB
        "-crf", "32",
        "-pix_fmt", "yuva420p", # Mendukung transparansi (alpha)
        "-an",              # Tanpa audio
        "-auto-alt-ref", "0",
        output_path
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False
        )
        if proc.returncode != 0:
            # Fallback tanpa yuva420p jika format asal tidak support alpha
            cmd_fallback = [
                FFMPEG_EXE, "-y", "-i", input_path, "-t", "3.0",
                "-vf", "scale=100:100:force_original_aspect_ratio=decrease,pad=100:100:(100-iw)/2:(100-ih)/2,fps=30",
                "-c:v", "libvpx-vp9", "-b:v", "180k", "-crf", "32", "-an", output_path
            ]
            proc = subprocess.run(cmd_fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if proc.returncode != 0:
                print(f"❌ Gagal konversi {os.path.basename(input_path)}: {proc.stderr.decode('utf-8', errors='ignore')[:200]}")
                return False

        # Verifikasi ukuran file
        size_kb = os.path.getsize(output_path) / 1024
        if size_kb > 256:
            print(f"⚠️ Peringatan: {os.path.basename(output_path)} berukuran {size_kb:.1f} KB (Melebihi batas 256 KB Telegram).")
        else:
            print(f"✅ Sukses: {os.path.basename(output_path)} ({size_kb:.1f} KB, 100x100 px, VP9)")
        return True

    except Exception as exc:
        print(f"❌ Exception saat konversi {input_path}: {exc}")
        return False


def main():
    os.makedirs(SOURCE_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=" * 65)
    print("🎨 TELEGRAM ANIMATED CUSTOM EMOJI (.WEBM) GENERATOR")
    print("=" * 65)
    print(f"📁 Folder Input : {SOURCE_DIR}")
    print(f"📁 Folder Output: {OUTPUT_DIR}")
    print("=" * 65)

    valid_extensions = (".gif", ".mp4", ".mov", ".avi", ".webp", ".png", ".jpg", ".jpeg")
    files_to_convert = [
        os.path.join(SOURCE_DIR, f)
        for f in os.listdir(SOURCE_DIR)
        if os.path.splitext(f)[1].lower() in valid_extensions
    ]

    if not files_to_convert:
        print(f"\n💡 Folder `emoji_sources/` masih kosong.")
        print(f"   Silakan letakkan file GIF/Video/Gambar Anda di: {SOURCE_DIR}")
        print(f"   Lalu jalankan ulang script ini: python scripts/convert_to_emoji_webm.py\n")
        return

    print(f"🔍 Ditemukan {len(files_to_convert)} file untuk dikonversi:\n")
    success_count = 0
    for file_path in files_to_convert:
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        out_file = os.path.join(OUTPUT_DIR, f"{base_name}.webm")
        if convert_file_to_telegram_webm(file_path, out_file):
            success_count += 1

    print("\n" + "=" * 65)
    print(f"🎉 Selesai: {success_count}/{len(files_to_convert)} file berhasil dikonversi ke .webm!")
    print(f"📂 Lokasi file: {OUTPUT_DIR}")
    print("=" * 65)
    print("\n📋 LANGKAH SELANJUTNYA KE TELEGRAM:")
    print("1. Buka bot @Stickers di Telegram.")
    print("2. Kirim perintah /newemojipack lalu pilih jenis 'Video'.")
    print("3. Beri nama pack Anda.")
    print("4. Kirim file .webm dari folder `emoji_output/` sebagai FILE (bukan foto/video biasa).")
    print("5. Ketik /publish dan tentukan link pack (misal: t.me/addemoji/NamaPackAnda).")
    print("6. Buka bot P2P Anda, lalu ketik /syncpack <link_pack_anda>.")
    print("   👉 Bot otomatis langsung memakai semua emoji bergerak tersebut!\n")


if __name__ == "__main__":
    main()
