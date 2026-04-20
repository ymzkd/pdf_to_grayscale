import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
import streamlit as st

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

PROBE_TIMEOUT = 60
ENCODE_TIMEOUT = 900

INPUT_EXTENSIONS = ["mp4", "mov", "avi", "mkv", "webm", "m4v", "gif"]

CODEC_CHOICES = {
    "H.264 (高互換)": "libx264",
    "H.265 (高圧縮)": "libx265",
}

SCALE_CHOICES = {
    "360p": 360,
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "元の解像度": None,
}

FPS_CHOICES = {
    "15": 15,
    "24": 24,
    "30": 30,
    "元のまま": None,
}

GIF_FPS_CHOICES = {
    "10": 10,
    "12": 12,
    "15": 15,
    "20": 20,
    "24": 24,
}


def format_size(bytes_size: int) -> str:
    if bytes_size < 1024:
        return f"{bytes_size} B"
    if bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.2f} KB"
    return f"{bytes_size / (1024 * 1024):.2f} MB"


def probe(path: str) -> dict:
    # ffmpeg は -i のみだとエラー終了するが、stderr にメタ情報が出る
    try:
        result = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", path],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ファイルの解析がタイムアウトしました ({PROBE_TIMEOUT} 秒)。")
    stderr = result.stderr
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0, "has_audio": False}

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        info["duration"] = h * 3600 + mi * 60 + s

    m = re.search(r"Stream[^\n]*Video:[^\n]*?(\d{2,5})x(\d{2,5})", stderr)
    if m:
        info["width"] = int(m.group(1))
        info["height"] = int(m.group(2))

    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", stderr)
    if m:
        info["fps"] = float(m.group(1))

    info["has_audio"] = bool(re.search(r"Stream[^\n]*Audio:", stderr))
    return info


@st.cache_data(show_spinner=False)
def _cached_probe(_data: bytes, digest: str, suffix: str) -> dict:
    # digest と suffix のみがキャッシュキー。bytes はハッシュをスキップ。
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(_data)
        path = tf.name
    try:
        return probe(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def probe_bytes(data: bytes, suffix: str) -> dict:
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
    return _cached_probe(data, digest, suffix)


def compute_target_size(src_w: int, src_h: int, max_long_edge: int | None) -> tuple[int, int]:
    if not max_long_edge or max(src_w, src_h) <= max_long_edge:
        # 偶数化のみ
        return (src_w - src_w % 2, src_h - src_h % 2)
    if src_w >= src_h:
        new_w = max_long_edge
        new_h = int(round(src_h * max_long_edge / src_w / 2)) * 2
    else:
        new_h = max_long_edge
        new_w = int(round(src_w * max_long_edge / src_h / 2)) * 2
    return max(new_w, 2), max(new_h, 2)


def run_ffmpeg(cmd: list[str], timeout: int = ENCODE_TIMEOUT) -> None:
    # 長時間のエンコードでも stderr をメモリに溜めないようファイルに流す
    log_fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(log_fd)
    try:
        with open(log_path, "wb") as stderr_f:
            try:
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_f,
                    stdin=subprocess.DEVNULL,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"ffmpeg がタイムアウトしました ({timeout} 秒)。"
                    "より短い動画にするか、解像度・fps を下げて再試行してください。"
                )
        if result.returncode != 0:
            with open(log_path, "r", errors="replace") as f:
                # 末尾だけ読み込めば十分
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(size - 8192, 0))
                tail = "\n".join(f.read().strip().splitlines()[-15:])
            raise RuntimeError(f"ffmpeg 実行に失敗しました:\n{tail}")
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass


def encode_mp4(
    input_path: str,
    output_path: str,
    video_kbps: int,
    audio_kbps: int,
    target_w: int,
    target_h: int,
    target_fps: float | None,
    codec: str,
    mute: bool,
    speed: float,
    workdir: str,
) -> None:
    vf_parts = []
    if speed != 1.0:
        vf_parts.append(f"setpts=PTS/{speed}")
    vf_parts.append(f"scale={target_w}:{target_h}:flags=lanczos")
    if target_fps:
        vf_parts.append(f"fps={target_fps}")
    vf = ",".join(vf_parts)

    passlog = os.path.join(workdir, "ffpass")
    base = [
        FFMPEG,
        "-y",
        "-i",
        input_path,
        "-vf",
        vf,
        "-c:v",
        codec,
        "-b:v",
        f"{video_kbps}k",
        "-maxrate",
        f"{int(video_kbps * 1.5)}k",
        "-bufsize",
        f"{int(video_kbps * 2)}k",
        "-pix_fmt",
        "yuv420p",
    ]

    pass1 = base + [
        "-pass",
        "1",
        "-passlogfile",
        passlog,
        "-an",
        "-f",
        "null",
        os.devnull,
    ]
    run_ffmpeg(pass1)

    pass2 = base + ["-pass", "2", "-passlogfile", passlog]
    if mute or audio_kbps <= 0:
        pass2 += ["-an"]
    else:
        pass2 += ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
    pass2 += ["-movflags", "+faststart", output_path]
    run_ffmpeg(pass2)


def encode_gif(
    input_path: str,
    output_path: str,
    target_w: int,
    target_h: int,
    fps: int,
    colors: int,
    speed: float,
    workdir: str,
) -> None:
    palette = os.path.join(workdir, "palette.png")
    scale_expr = f"scale={target_w}:{target_h}:flags=lanczos"
    speed_expr = f"setpts=PTS/{speed}," if speed != 1.0 else ""
    pal_cmd = [
        FFMPEG,
        "-y",
        "-i",
        input_path,
        "-vf",
        f"{speed_expr}fps={fps},{scale_expr},palettegen=max_colors={colors}",
        palette,
    ]
    run_ffmpeg(pal_cmd)

    gif_cmd = [
        FFMPEG,
        "-y",
        "-i",
        input_path,
        "-i",
        palette,
        "-filter_complex",
        f"{speed_expr}fps={fps},{scale_expr}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",
        "-loop",
        "0",
        output_path,
    ]
    run_ffmpeg(gif_cmd)


def compress_to_mp4(
    input_path: str,
    output_path: str,
    target_size_bytes: int,
    info: dict,
    max_long_edge: int | None,
    max_fps: float | None,
    codec: str,
    mute: bool,
    speed: float,
    workdir: str,
    status_cb,
) -> None:
    effective_duration = max(info["duration"] / speed, 0.1)
    has_audio = info["has_audio"] and not mute
    audio_kbps = 96 if has_audio else 0

    target_w, target_h = compute_target_size(info["width"], info["height"], max_long_edge)
    src_fps = info["fps"] or 30.0
    target_fps = min(src_fps, max_fps) if max_fps else src_fps

    safety = 0.95
    total_kbps = max(int(target_size_bytes * 8 / 1000 / effective_duration * safety), 64)
    video_kbps = max(total_kbps - audio_kbps, 32)

    attempt = 0
    max_attempts = 4
    while True:
        attempt += 1
        status_cb(f"エンコード中 (試行 {attempt}): {target_w}x{target_h}, {int(target_fps)}fps, {video_kbps}kbps")

        # 極端に低ビットレートの場合は解像度・fps を段階的に下げる
        if video_kbps < 200 and max(target_w, target_h) > 360:
            target_w, target_h = compute_target_size(target_w, target_h, 480)
        if video_kbps < 150 and target_fps > 15:
            target_fps = 15

        encode_mp4(
            input_path,
            output_path,
            video_kbps,
            audio_kbps,
            target_w,
            target_h,
            target_fps,
            codec,
            mute,
            speed,
            workdir,
        )
        size = os.path.getsize(output_path)
        if size <= target_size_bytes or attempt >= max_attempts:
            return
        # 目標超過: 実測サイズから再計算して 0.9 倍の安全率
        ratio = target_size_bytes / size
        video_kbps = max(int(video_kbps * ratio * 0.9), 32)


def compress_to_gif(
    input_path: str,
    output_path: str,
    target_size_bytes: int,
    info: dict,
    max_long_edge: int | None,
    fps: int,
    speed: float,
    workdir: str,
    status_cb,
) -> None:
    long_edge = max_long_edge or max(info["width"], info["height"])
    current_fps = fps

    steps = [
        # (scale, fps, colors) の縮退順
        (long_edge, current_fps, 256),
        (long_edge, current_fps, 128),
        (long_edge, max(current_fps - 3, 8), 128),
        (min(long_edge, 480), max(current_fps - 5, 8), 128),
        (min(long_edge, 360), max(current_fps - 7, 8), 96),
        (min(long_edge, 320), 10, 64),
    ]

    for i, (edge, f, c) in enumerate(steps, 1):
        target_w, target_h = compute_target_size(info["width"], info["height"], edge)
        status_cb(f"GIF 生成中 (試行 {i}): {target_w}x{target_h}, {f}fps, {c} colors")
        encode_gif(input_path, output_path, target_w, target_h, f, c, speed, workdir)
        last_size = os.path.getsize(output_path)
        if last_size <= target_size_bytes:
            return
    # 最後の試行結果をそのまま返す（超過時は呼び出し側で警告）


st.title("動画圧縮ツール")
st.caption(
    "mp4 / gif などを指定サイズ以下に圧縮します。2-pass エンコードでなるべく画質を保ちます。"
    "音声は常にミュートされます（資料貼り付け用途）。"
)

uploaded = st.file_uploader("動画ファイルを選択", type=INPUT_EXTENSIONS)

if uploaded is not None:
    input_bytes = uploaded.getvalue()
    input_size = len(input_bytes)
    suffix = Path(uploaded.name).suffix or ".bin"

    try:
        src_info = probe_bytes(input_bytes, suffix)
    except Exception as exc:
        st.error(f"ファイルの解析に失敗しました: {exc}")
        st.stop()

    if src_info["duration"] <= 0 or src_info["width"] == 0:
        st.error("動画の情報を取得できませんでした。別のファイルをお試しください。")
        st.stop()

    st.subheader("入力ファイル情報")
    info_cols = st.columns(5)
    info_cols[0].metric("サイズ", format_size(input_size))
    info_cols[1].metric("解像度", f"{src_info['width']}×{src_info['height']}")
    info_cols[2].metric("長さ", f"{src_info['duration']:.1f} 秒")
    info_cols[3].metric("fps", f"{src_info['fps']:.1f}")
    info_cols[4].metric("音声", "有" if src_info["has_audio"] else "無")
    bitrate_kbps = int(input_size * 8 / 1000 / max(src_info["duration"], 0.1))
    st.caption(f"推定ビットレート: 約 {bitrate_kbps} kbps")

    st.divider()
    st.subheader("変換設定")

    col_a, col_b, col_c = st.columns(3)
    output_format = col_a.radio("出力形式", ("mp4", "gif"), horizontal=True)
    target_size_mb = col_b.number_input(
        "目標サイズ (MB)", min_value=0.5, max_value=100.0, value=5.0, step=0.5
    )
    speed = col_c.number_input(
        "再生速度倍率",
        min_value=0.5,
        max_value=8.0,
        value=1.0,
        step=0.25,
        help="1.0 で等速。2.0 にすると 2 倍速で再生され、動画時間が半分になります。",
    )
    if speed != 1.0:
        st.caption(
            f"出力動画の長さ: 約 {src_info['duration'] / speed:.1f} 秒 "
            f"（{speed:g}x 再生）"
        )

    # 音声は常にミュート
    mute = True

    with st.expander("詳細設定", expanded=False):
        if output_format == "mp4":
            scale_label = st.selectbox(
                "最大解像度（長辺）", list(SCALE_CHOICES.keys()), index=2
            )
            fps_label = st.selectbox("最大 fps", list(FPS_CHOICES.keys()), index=2)
            codec_label = st.radio(
                "コーデック", list(CODEC_CHOICES.keys()), horizontal=True
            )
            max_long_edge = SCALE_CHOICES[scale_label]
            max_fps = FPS_CHOICES[fps_label]
            codec = CODEC_CHOICES[codec_label]
        else:
            scale_label = st.selectbox(
                "最大解像度（長辺）", list(SCALE_CHOICES.keys()), index=1
            )
            fps_label = st.selectbox("fps", list(GIF_FPS_CHOICES.keys()), index=2)
            max_long_edge = SCALE_CHOICES[scale_label]
            gif_fps = GIF_FPS_CHOICES[fps_label]
            codec = None
            max_fps = None

    if st.button("圧縮する", type="primary"):
        target_bytes = int(target_size_mb * 1024 * 1024)

        with tempfile.TemporaryDirectory() as workdir:
            input_path = os.path.join(workdir, f"input{suffix}")
            output_name = Path(uploaded.name).stem + f"_compressed.{output_format}"
            output_path = os.path.join(workdir, output_name)

            with open(input_path, "wb") as f:
                f.write(input_bytes)

            try:
                with st.status("エンコード中...", expanded=True) as status:
                    def status_cb(msg: str) -> None:
                        status.write(msg)

                    if output_format == "mp4":
                        compress_to_mp4(
                            input_path,
                            output_path,
                            target_bytes,
                            src_info,
                            max_long_edge,
                            max_fps,
                            codec,
                            mute,
                            speed,
                            workdir,
                            status_cb,
                        )
                    else:
                        compress_to_gif(
                            input_path,
                            output_path,
                            target_bytes,
                            src_info,
                            max_long_edge,
                            gif_fps,
                            speed,
                            workdir,
                            status_cb,
                        )
                    status.update(label="エンコード完了", state="complete")

                with open(output_path, "rb") as f:
                    output_bytes = f.read()

            except RuntimeError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"予期せぬエラー: {exc}")
            else:
                output_size = len(output_bytes)
                ratio = output_size / input_size if input_size else 0
                sign = "-" if output_size < input_size else "+"

                col1, col2 = st.columns(2)
                col1.metric("変換前", format_size(input_size))
                col2.metric(
                    "変換後",
                    format_size(output_size),
                    delta=f"{sign}{format_size(abs(output_size - input_size))} ({ratio:.0%})",
                    delta_color="inverse",
                )

                if output_size > target_bytes:
                    st.warning(
                        f"目標サイズ {target_size_mb:.1f}MB に収まりませんでした。"
                        "解像度や fps を下げて再試行してください。"
                    )
                else:
                    st.success("圧縮に成功しました。")

                if output_format == "mp4":
                    st.video(output_bytes)
                else:
                    st.image(output_bytes)

                mime = "video/mp4" if output_format == "mp4" else "image/gif"
                st.download_button(
                    label=f"{output_format.upper()} をダウンロード",
                    data=output_bytes,
                    file_name=output_name,
                    mime=mime,
                    type="primary",
                )
