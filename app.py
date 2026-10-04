from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from subtitle_app.core import (Cancelled, DEFAULT_CODEX_MODEL, LANGUAGES, NO_WINDOW, ROOT, VIDEO_EXTENSIONS,
                              Pipeline, Settings, codex_status, collect_inputs, find_codex, output_conflicts)


def enable_dpi() -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


class Application:
    def __init__(self, root: tk.Tk, initial: list[str] | None = None, *, recursive: bool = False):
        self.root, self.settings = root, Settings.load()
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.paths: list[Path] = []
        self.root.title("영상 자막 생성 · Whisper + Codex")
        self.root.geometry("1040x830")
        self.root.minsize(890, 690)
        self.root.configure(background="#f4f6fa")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=("맑은 고딕", 10))
        style.configure("TFrame", background="#f4f6fa")
        style.configure("TLabel", background="#f4f6fa")
        style.configure("TLabelframe", background="#f4f6fa")
        style.configure("TLabelframe.Label", background="#f4f6fa", font=("맑은 고딕", 10, "bold"))
        style.configure("Title.TLabel", font=("맑은 고딕", 20, "bold"), foreground="#182b49")
        style.configure("Muted.TLabel", foreground="#546277")
        style.configure("Accent.TButton", background="#2463eb", foreground="white", padding=(16, 8), font=("맑은 고딕", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#164ac1"), ("disabled", "#b7c1d1")])
        style.configure("TButton", padding=(10, 6))
        style.configure("Treeview", rowheight=30, background="white", fieldbackground="white")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main = ttk.Frame(root, padding=22)
        main.grid(sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=2)
        main.rowconfigure(5, weight=1)
        ttk.Label(main, text="영상 폴더에 자막 만들기", style="Title.TLabel").grid(row=0, sticky="w")
        ttk.Label(main, text="영상 선택 → 내장 자막 추출 또는 로컬 음성 인식 → Codex 번역 → SRT 저장", style="Muted.TLabel").grid(row=1, sticky="w", pady=(4, 16))
        files = ttk.LabelFrame(main, text="처리할 파일", padding=10)
        files.grid(row=2, sticky="nsew")
        files.columnconfigure(0, weight=1)
        files.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(files, columns=("path", "state"), show="headings", selectmode="extended", height=6)
        self.tree.heading("path", text="영상 / SRT 파일")
        self.tree.heading("state", text="진행 상태")
        self.tree.column("path", width=740)
        self.tree.column("state", width=135, stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(files, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        buttons = ttk.Frame(files)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.file_buttons = []
        for title, action in (("영상 선택", self.add_files), ("폴더 선택", self.add_folder), ("SRT 번역", self.add_srt), ("선택 제거", self.remove_selected)):
            button = ttk.Button(buttons, text=title, command=action)
            button.pack(side="left", padx=(0, 6))
            self.file_buttons.append(button)
        sample_button = ttk.Button(buttons, text="무료 샘플 검증", command=self.start_verification)
        sample_button.pack(side="left", padx=(0, 6))
        self.file_buttons.append(sample_button)
        ttk.Button(buttons, text="영상 폴더 열기", command=self.open_folder).pack(side="right")
        self.report_button = ttk.Button(buttons, text="검증 보고서", command=self.open_report)
        self.report_button.pack(side="right", padx=(0, 6))
        self.controls = []
        self.folder_path = tk.StringVar()
        self.recursive = tk.BooleanVar(value=recursive)
        folder_row = ttk.Frame(files)
        folder_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        folder_row.columnconfigure(1, weight=1)
        ttk.Label(folder_row, text="폴더 경로").grid(row=0, column=0, padx=(0, 8))
        folder_entry = ttk.Entry(folder_row, textvariable=self.folder_path)
        folder_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        nested_check = ttk.Checkbutton(folder_row, text="하위 폴더 포함", variable=self.recursive)
        nested_check.grid(row=0, column=2, padx=(0, 10))
        folder_start = ttk.Button(folder_row, text="폴더 자막 생성", command=self.start_folder)
        folder_start.grid(row=0, column=3)
        self.file_buttons.append(folder_start)
        self.controls.extend(((folder_entry, "normal"), (nested_check, "normal")))
        options = ttk.LabelFrame(main, text="설정", padding=12)
        options.grid(row=3, sticky="ew", pady=(14, 0))
        for column in range(4):
            options.columnconfigure(column, weight=1)
        self.source = tk.StringVar(value=self.label(self.settings.source_language))
        self.target = tk.StringVar(value=self.label(self.settings.target_language))
        self.model = tk.StringVar(value=self.settings.whisper_model)
        self.device = tk.StringVar(value=self.settings.device)
        self.codex_model = tk.StringVar(value=self.settings.codex_model)
        self.audio_track = tk.StringVar(value=str(self.settings.audio_track))
        self.translate = tk.BooleanVar(value=self.settings.translate)
        self.embedded = tk.BooleanVar(value=self.settings.prefer_embedded)
        self.overwrite = tk.BooleanVar(value=self.settings.overwrite)
        self.speech_filter = tk.BooleanVar(value=self.settings.speech_filter)
        choices = (("원문 언어", self.source, ["자동 감지 (auto)"] + [self.label(code) for code in LANGUAGES]),
                   ("번역 언어", self.target, [self.label(code) for code in LANGUAGES]),
                   ("Whisper 모델", self.model, ["turbo", "large-v3", "medium", "small", "base", "tiny"]),
                   ("인식 장치", self.device, ["auto", "cuda", "cpu"]))
        for column, (title, variable, values) in enumerate(choices):
            ttk.Label(options, text=title).grid(row=0, column=column, sticky="w")
            combo = ttk.Combobox(options, textvariable=variable, values=values, state="readonly", width=20)
            combo.grid(row=1, column=column, sticky="ew", padx=(0, 10), pady=(4, 10))
            self.controls.append((combo, "readonly"))
        ttk.Label(options, text="Codex 모델 (기본 GPT-6.1 Sol)").grid(row=2, column=0, columnspan=2, sticky="w")
        entry = ttk.Entry(options, textvariable=self.codex_model)
        entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(0, 10), pady=4)
        self.controls.append((entry, "normal"))
        ttk.Label(options, text="오디오 트랙 (첫 번째 = 0)").grid(row=2, column=2, sticky="w")
        track = ttk.Spinbox(options, from_=0, to=20, textvariable=self.audio_track, width=8)
        track.grid(row=3, column=2, sticky="w", pady=4)
        self.controls.append((track, "normal"))
        self.login_button = ttk.Button(options, text="Codex 로그인", command=self.login)
        self.login_button.grid(row=3, column=3, sticky="e")
        checks = ttk.Frame(options)
        checks.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 4))
        for title, variable in (("Codex로 번역", self.translate), ("내장 텍스트 자막 우선", self.embedded), ("기존 결과 덮어쓰기", self.overwrite), ("무음 필터(VAD)", self.speech_filter)):
            check = ttk.Checkbutton(checks, text=title, variable=variable)
            check.pack(side="left", padx=(0, 18))
            self.controls.append((check, "normal"))
        ttk.Label(options, text="용어 / 인물명 지침 (예: John=존, 등장인물 사이에는 반말 사용)").grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 2))
        self.glossary = tk.Text(options, height=2, font=("맑은 고딕", 10), relief="solid", borderwidth=1, wrap="word")
        self.glossary.grid(row=6, column=0, columnspan=4, sticky="ew")
        self.glossary.insert("1.0", self.settings.glossary)
        self.controls.append((self.glossary, "normal"))
        self.notice = ttk.Label(main, text="Whisper는 로컬에서 실행됩니다. 번역 시 대사 텍스트가 Codex에 전송되며 계정 사용량을 소비합니다.", style="Muted.TLabel", wraplength=970)
        self.notice.grid(row=4, sticky="w", pady=(9, 7))
        self.log_box = ScrolledText(main, height=6, state="disabled", font=("맑은 고딕", 9), background="#fff", relief="solid", borderwidth=1)
        self.log_box.grid(row=5, sticky="nsew")
        footer = ttk.Frame(main)
        footer.grid(row=6, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="파일을 선택하세요. run.bat에 영상 파일을 끌어 놓아도 됩니다.")
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.bar = ttk.Progressbar(footer, maximum=100)
        self.bar.grid(row=1, column=0, sticky="ew", padx=(0, 16), pady=(6, 0))
        self.stop_button = ttk.Button(footer, text="중지", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=1, rowspan=2, padx=(0, 8))
        self.start_button = ttk.Button(footer, text="자막 생성", style="Accent.TButton", command=self.start)
        self.start_button.grid(row=0, column=2, rowspan=2)
        self.append_paths(initial or [])
        initial_folders = [Path(raw) for raw in initial or [] if Path(raw).is_dir()]
        if initial_folders:
            self.folder_path.set(str(initial_folders[0].resolve()))
        self.root.after(100, self.poll)

    @staticmethod
    def label(code: str) -> str:
        return "자동 감지 (auto)" if code == "auto" else f"{LANGUAGES.get(code, code)} ({code})"

    @staticmethod
    def code(label: str) -> str:
        return label.rsplit("(", 1)[-1].rstrip(")").strip()

    def append_paths(self, raw: list[str]) -> None:
        if self.busy:
            return
        try:
            discovered = collect_inputs(raw, recursive=self.recursive.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("파일 확인", str(exc))
            return
        for path in discovered:
            if path not in self.paths:
                self.paths.append(path)
                self.tree.insert("", "end", iid=str(len(self.paths) - 1), values=(str(path), "대기"))
        self.status.set(f"{len(self.paths)}개 파일 · 결과는 각 파일과 같은 폴더에 저장됩니다.")

    def add_files(self) -> None:
        self.append_paths(list(filedialog.askopenfilenames(title="영상 선택", filetypes=[("영상", " ".join(f"*{extension}" for extension in sorted(VIDEO_EXTENSIONS))), ("모든 파일", "*.*")])))

    def add_srt(self) -> None:
        self.append_paths(list(filedialog.askopenfilenames(title="번역할 원문 SRT 선택", filetypes=[("SRT 자막", "*.srt")])))

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="영상이 있는 폴더 선택")
        if folder:
            self.folder_path.set(folder)
            self.append_paths([folder])

    def start_folder(self) -> None:
        if self.busy:
            return
        raw_folder = self.folder_path.get().strip().strip('"')
        folder = Path(raw_folder).expanduser()
        if not raw_folder or not folder.is_dir():
            messagebox.showerror("폴더 확인", "존재하는 영상 폴더의 경로를 입력하거나 ‘폴더 선택’을 누르세요.")
            return
        try:
            paths = collect_inputs([folder], recursive=self.recursive.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("폴더 확인", str(exc))
            return
        if not paths:
            messagebox.showinfo("폴더 확인", "지원하는 영상 파일이 없습니다. 하위 폴더에 있다면 ‘하위 폴더 포함’을 선택하세요.")
            return
        self.folder_path.set(str(folder.resolve()))
        self.tree.delete(*self.tree.get_children())
        self.paths = []
        self.append_paths([str(path) for path in paths])
        self.start()

    def remove_selected(self) -> None:
        selected = {int(item) for item in self.tree.selection()}
        remaining = [path for index, path in enumerate(self.paths) if index not in selected]
        self.tree.delete(*self.tree.get_children())
        self.paths = []
        self.append_paths([str(path) for path in remaining])

    def open_folder(self) -> None:
        selected = self.tree.selection()
        if self.paths:
            path = self.paths[int(selected[0])] if selected else self.paths[0]
            os.startfile(str(path.parent)) if os.name == "nt" else subprocess.Popen(["xdg-open", str(path.parent)])

    def read_settings(self) -> Settings:
        settings = Settings(source_language=self.code(self.source.get()), target_language=self.code(self.target.get()),
                            whisper_model=self.model.get(), device=self.device.get(), codex_model=self.codex_model.get().strip(),
                            translate=self.translate.get(), prefer_embedded=self.embedded.get(), overwrite=self.overwrite.get(),
                            glossary=self.glossary.get("1.0", "end").strip(), audio_track=int(self.audio_track.get()),
                            speech_filter=self.speech_filter.get())
        settings.validate()
        return settings

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.start_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.login_button.configure(state="disabled" if busy else "normal")
        for button in self.file_buttons:
            button.configure(state="disabled" if busy else "normal")
        for widget, state in self.controls:
            widget.configure(state="disabled" if busy else state)

    def start(self) -> None:
        if self.busy:
            return
        if not self.paths:
            messagebox.showinfo("영상 선택", "먼저 영상 또는 SRT 파일을 선택하세요.")
            return
        try:
            settings = self.read_settings()
            settings.save()
        except (ValueError, OSError) as exc:
            messagebox.showerror("설정 확인", str(exc))
            return
        self.cancel.clear()
        self.set_busy(True)
        self.bar["value"] = 0
        self.status.set("처리 시작…")
        threading.Thread(target=self.worker, args=(settings, list(self.paths)), daemon=True).start()

    def start_verification(self) -> None:
        if self.busy:
            return
        try:
            settings = self.read_settings()
        except ValueError as exc:
            messagebox.showerror("설정 확인", str(exc))
            return
        self.cancel.clear()
        self.set_busy(True)
        self.bar["value"] = 0
        self.status.set("무료 샘플 다운로드·검증 · 영어 → 한국어")
        threading.Thread(target=self.verification_worker, args=(settings,), daemon=True).start()

    def verification_worker(self, settings: Settings) -> None:
        try:
            from subtitle_app.verification import run_verification
            report = run_verification(settings, cancel=self.cancel, log=lambda text: self.events.put(("log", text)),
                                      progress=lambda value: self.events.put(("progress", value)))
            self.events.put(("verification_finished", report))
        except Exception as exc:
            self.events.put(("log", f"[검증 실패] {exc}"))
            self.events.put(("finished", 0, 1, 0, self.cancel.is_set()))

    def open_report(self) -> None:
        from subtitle_app.core import read_json
        latest = read_json(ROOT / "test_runs" / "latest.json", {})
        reference = latest.get("report") if isinstance(latest, dict) else None
        path = Path(reference) if isinstance(reference, str) and reference else ROOT / "test_runs" / "missing-report"
        if not path.is_file():
            messagebox.showinfo("검증 보고서", "먼저 ‘무료 샘플 검증’을 실행하세요.")
            return
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def worker(self, settings: Settings, paths: list[Path]) -> None:
        done, failed, review, skipped = 0, 0, 0, 0
        try:
            pipeline = Pipeline(settings, cancel=self.cancel, log=lambda text: self.events.put(("log", text)),
                                progress=lambda value: self.events.put(("progress", value)))
            conflicts = output_conflicts(paths, target_language=settings.target_language, translate=settings.translate)
            for index, path in enumerate(paths):
                if self.cancel.is_set():
                    break
                if path in conflicts:
                    failed += 1
                    self.events.put(("state", index, "이름 중복"))
                    self.events.put(("log", f"[실패] {path} · 다른 입력과 자막/보고서 이름이 겹칩니다. 입력 이름을 구분하세요."))
                    continue
                if path.suffix.lower() in VIDEO_EXTENSIONS and path.with_suffix(".srt").exists() and not settings.overwrite:
                    skipped += 1
                    self.events.put(("state", index, "기존 자막 있음"))
                    self.events.put(("log", f"[건너뜀] {path.name} · {path.with_suffix('.srt').name}이 이미 있습니다."))
                    continue
                self.events.put(("state", index, "처리 중"))
                self.events.put(("status", f"{index + 1}/{len(paths)} · {path.name}"))
                try:
                    result = pipeline.process(path)
                    done += 1
                    review += int(result.review_count > 0)
                    state = "대사 없음" if not result.cue_count else "확인 필요" if result.review_count else "완료"
                    self.events.put(("state", index, state))
                except Cancelled as exc:
                    self.events.put(("log", str(exc)))
                    self.events.put(("state", index, "중지"))
                    break
                except Exception as exc:
                    failed += 1
                    self.events.put(("state", index, "실패"))
                    self.events.put(("log", f"[실패] {path.name}: {exc}"))
        except Exception as exc:
            failed += 1
            self.events.put(("log", f"[일괄 처리 실패] {exc}"))
        finally:
            self.events.put(("finished", done, failed, review, self.cancel.is_set(), skipped))

    def poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", event[1] + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "progress":
                    self.bar["value"] = event[1] * 100
                elif kind == "state":
                    self.tree.set(str(event[1]), "state", event[2])
                elif kind == "status":
                    self.status.set(event[1])
                elif kind == "finished":
                    self.set_busy(False)
                    if not event[4]:
                        self.bar["value"] = 100
                    title = "중지됨" if event[4] else "처리 종료"
                    summary = f"{title} · 완료 {event[1]}개 / 실패 {event[2]}개 / 확인 필요 {event[3]}개"
                    if len(event) > 5:
                        summary += f" / 건너뜀 {event[5]}개"
                    self.status.set(summary)
                    self.events.put(("log", summary))
                elif kind == "verification_finished":
                    self.set_busy(False)
                    labels = {"pass": "통과", "warning": "주의", "fail": "실패"}
                    self.status.set(f"샘플 검증 {labels[event[1]['status']]} · ‘검증 보고서’에서 상세 결과를 확인하세요.")
                    self.events.put(("log", f"보고서: {event[1]['report_path']}"))
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def stop(self) -> None:
        self.cancel.set()
        self.status.set("중지 요청됨 · 음성 인식은 현재 구간이 끝나면 멈춥니다.")
        self.stop_button.configure(state="disabled")

    def login(self) -> None:
        try:
            subprocess.Popen([find_codex(), "login"], creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0)
            self.events.put(("log", "로그인 창에서 ChatGPT 계정으로 로그인한 뒤 자막 생성을 실행하세요."))
        except Exception as exc:
            messagebox.showerror("Codex 로그인", str(exc))

    def close(self) -> None:
        if self.busy:
            messagebox.showinfo("처리 중", "먼저 ‘중지’를 누르고 처리가 멈추면 창을 닫으세요.")
            return
        try:
            self.read_settings().save()
        except (ValueError, OSError):
            pass
        self.root.destroy()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="영상과 같은 폴더에 원문 및 Codex 번역 SRT 생성")
    parser.add_argument("paths", nargs="*", help="영상/SRT 파일 또는 영상 폴더")
    parser.add_argument("--cli", action="store_true", help="창을 열지 않고 처리")
    parser.add_argument("--source", default="auto", help="원문 언어 코드, 기본 auto")
    parser.add_argument("--target", default="ko", help="번역 언어 코드, 기본 ko")
    parser.add_argument("--model", default="turbo", help="Whisper 모델, 기본 turbo")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL, help=f"Codex 모델 이름, 기본 {DEFAULT_CODEX_MODEL}")
    parser.add_argument("--original-only", action="store_true", help="Codex를 사용하지 않고 원문 자막만 생성")
    parser.add_argument("--force-whisper", action="store_true", help="내장 자막 대신 음성 인식")
    parser.add_argument("--overwrite", action="store_true", help="기존 결과를 덮어씀")
    parser.add_argument("--audio-track", type=int, default=0)
    parser.add_argument("--glossary", default="", help="인물명/용어/말투 지침")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--timeout", type=int, default=600, help="Codex 배치 응답 최대 대기(초)")
    parser.add_argument("--check", action="store_true", help="설치 및 Codex 로그인 확인")
    parser.add_argument("--speech-filter", action="store_true", help="무음 필터(VAD) 사용. 음악 위 대사 누락에 주의")
    parser.add_argument("--verify", action="store_true", help="공개 샘플 다운로드 후 실제 인식·번역 검증")
    parser.add_argument("--download-sample", action="store_true", help="무료 샘플만 다운로드하고 무결성 검사")
    parser.add_argument("--recursive", action="store_true", help="입력 폴더의 하위 폴더 영상도 포함")
    args = parser.parse_args()
    if args.check:
        from subtitle_app.core import configure_cuda_paths, ffmpeg
        configure_cuda_paths()
        import ctranslate2
        import faster_whisper
        print("Whisper:", faster_whisper.__version__)
        print("FFmpeg:", ffmpeg())
        print("CUDA devices:", ctranslate2.get_cuda_device_count())
        print("Codex:", codex_status(threading.Event()))
        return 0
    if args.download_sample:
        from subtitle_app.verification import download_sample
        print(download_sample(threading.Event()))
        return 0
    settings = Settings(source_language=args.source, target_language=args.target, whisper_model=args.model,
                        device=args.device, translate=not args.original_only, prefer_embedded=not args.force_whisper,
                        overwrite=args.overwrite, codex_model=args.codex_model, glossary=args.glossary,
                        audio_track=args.audio_track, batch_size=args.batch_size, timeout=args.timeout,
                        speech_filter=args.speech_filter)
    if args.cli or args.verify:
        try:
            settings.validate()
        except ValueError as exc:
            parser.error(str(exc))
    if args.verify:
        from subtitle_app.verification import run_verification
        report = run_verification(settings)
        print(json.dumps({"status": report["status"], "report": report["report_path"]}, ensure_ascii=False))
        return {"pass": 0, "fail": 1, "warning": 2}[report["status"]]
    if not args.cli:
        enable_dpi()
        root = tk.Tk()
        Application(root, args.paths, recursive=args.recursive)
        root.mainloop()
        return 0
    try:
        paths = collect_inputs(args.paths, recursive=args.recursive)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if not paths:
        parser.error("처리할 영상/SRT 또는 폴더를 지정하세요.")
    pipeline = Pipeline(settings)
    conflicts = output_conflicts(paths, target_language=settings.target_language, translate=settings.translate)
    failures = 0
    for path in paths:
        try:
            if path in conflicts:
                raise ValueError("다른 입력과 생성할 자막/보고서 이름이 겹칩니다. 입력 이름을 구분하세요.")
            if path.suffix.lower() in VIDEO_EXTENSIONS and path.with_suffix(".srt").exists() and not settings.overwrite:
                print(json.dumps({"source": str(path), "status": "skipped", "reason": "기존 자막 있음",
                                  "subtitle": str(path.with_suffix(".srt"))}, ensure_ascii=False))
                continue
            result = pipeline.process(path)
            print(json.dumps({"original": str(result.original), "translated": str(result.translated) if result.translated else None,
                              "subtitle": str(result.matched_subtitle) if result.matched_subtitle else None,
                              "report": str(result.report), "review_count": result.review_count}, ensure_ascii=False))
            failures += int(result.review_count > 0)
        except KeyboardInterrupt:
            pipeline.cancel.set()
            return 130
        except Exception as exc:
            print(f"[실패] {path.name}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
