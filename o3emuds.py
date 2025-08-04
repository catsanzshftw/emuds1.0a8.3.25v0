import tkinter as tk
from tkinter import filedialog, font, ttk
import random, os, ctypes, threading, time, sys
from PIL import Image, ImageTk  # Pillow is required for framebuffer rendering

"""
emuds_nogba.py — GUI frontend for a Nintendo DS emulator with a pluggable
backend architecture. This file adds an extensible core-emulation layer that can
optionally link against a native **no$gba** shared library when present.

If the real backend is not available, the stub implementation gracefully
fallbacks to synthetic frames and registers so that the UI remains interactive.

How to hook up the real no$gba core (high-level):
-------------------------------------------------
1. Obtain or build a shared library version of no$gba (e.g. `nogba.dll` on
   Windows or `libnogba.so` on Linux via Wine/MinGW cross-compile).  The library
   must export at least the following C API:

       int  nogba_init();
       void nogba_reset();
       int  nogba_load_rom(const char *path);
       void nogba_run_frame();
       void nogba_get_arm9_regs(uint32_t dst[17]);   // R0-R15 + CPSR
       void nogba_get_arm7_regs(uint32_t dst[17]);
       void nogba_get_framebuffer_top(uint16_t *dst);   // 256×192 RGB565
       void nogba_get_framebuffer_bot(uint16_t *dst);

2. Place the compiled library in the same directory as this script or in a
   location discoverable by the system loader (e.g. on *PATH*).
3. Run `python emuds_nogba.py` — the GUI will automatically attempt to link the
   library; if successful the status bar shows **no$gba core loaded**.

Note: Implementing a legal, redistributable no$gba DLL is left to the user due
      to licensing constraints.  You can swap in any other core that exposes
      the minimal API described above (e.g. a custom Unicorn-engine build).
"""

FB_W, FB_H = 256, 192

class EmulatorCore:
    """Abstract base class — implements a null core that produces random data."""

    def __init__(self):
        self.rom_path: str | None = None
        self.running = False
        self._frame = 0

        # Pre-allocate numpy-like buffers with plain Python lists for portability.
        self.arm9 = [0] * 16  # R0-R15
        self.arm7 = [0] * 16
        self.cpsr9 = 0x1F
        self.cpsr7 = 0x1F
        self.fb_top = Image.new("RGB", (FB_W, FB_H), "black")
        self.fb_bot = Image.new("RGB", (FB_W, FB_H), "black")

    # ---------------------------------------------------------------------
    # Public API expected by the GUI layer
    # ---------------------------------------------------------------------
    def load_rom(self, path: str):
        """Return True on success."""
        self.rom_path = path
        return True

    def reset(self):
        self._frame = 0
        self.running = False

    def run_frame(self):
        """Advance one emulation frame."""
        self._frame += 1
        self._fake_registers()
        self._fake_framebuffers()

    def get_registers(self):
        return self.arm9, self.arm7, self.cpsr9, self.cpsr7

    def get_framebuffers(self):
        return self.fb_top, self.fb_bot

    # ------------------------------------------------------------------
    # Stub helpers — generate synthetic data so the GUI stays alive
    # ------------------------------------------------------------------
    def _fake_registers(self):
        self.arm9 = [random.randint(0, 0xFFFFFFFF) for _ in range(16)]
        self.arm7 = [random.randint(0, 0xFFFFFFFF) for _ in range(16)]
        self.cpsr9 = random.choice([0x10, 0x1F, 0x13])
        self.cpsr7 = random.choice([0x10, 0x1F, 0x13])

    def _fake_framebuffers(self):
        c1 = (60 + 10 * (self._frame % 6), 50, 255)
        c2 = (255, 60 + 7 * (self._frame % 8), 80)
        for y in range(FB_H):
            for x in range(FB_W):
                self.fb_top.putpixel((x, y), c1 if (y // 8) % 2 == 0 else (42, 54, 84))
                self.fb_bot.putpixel((x, y), c2 if (y // 8) % 2 == 0 else (34, 34, 34))


class NoGbaCore(EmulatorCore):
    """Concrete implementation that bridges to a native no$gba shared library."""

    def __init__(self):
        super().__init__()
        self._lib = self._load_library()
        if self._lib:
            self._init_functions()
            if self._lib.nogba_init() != 0:
                raise RuntimeError("no$gba: initialization failed")

    # -------------------------- FFI setup ------------------------------
    def _load_library(self):
        candidates = [
            "nogba.dll" if os.name == "nt" else "libnogba.so",
            os.path.join(os.path.dirname(__file__), "nogba.dll"),
            os.path.join(os.path.dirname(__file__), "libnogba.so"),
        ]
        for path in candidates:
            try:
                return ctypes.CDLL(path)
            except OSError:
                continue
        print("[WARN] no$gba shared library not found — falling back to stub mode.")
        return None

    def _init_functions(self):
        self._lib.nogba_init.restype = ctypes.c_int
        self._lib.nogba_reset.restype = None
        self._lib.nogba_load_rom.argtypes = [ctypes.c_char_p]
        self._lib.nogba_load_rom.restype = ctypes.c_int
        self._lib.nogba_run_frame.restype = None
        self._lib.nogba_get_arm9_regs.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        self._lib.nogba_get_arm7_regs.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
        self._lib.nogba_get_framebuffer_top.argtypes = [ctypes.POINTER(ctypes.c_uint16)]
        self._lib.nogba_get_framebuffer_bot.argtypes = [ctypes.POINTER(ctypes.c_uint16)]

    # -------------------------- Public API ----------------------------

    def load_rom(self, path: str):
        if not self._lib:
            return super().load_rom(path)
        if self._lib.nogba_load_rom(path.encode("utf-8")) != 0:
            return False
        self.rom_path = path
        return True

    def reset(self):
        if self._lib:
            self._lib.nogba_reset()
        super().reset()

    def run_frame(self):
        if not self._lib:
            return super().run_frame()

        self._lib.nogba_run_frame()
        self._pull_registers()
        self._pull_framebuffers()
        self._frame += 1

    # -------------------------- Helpers -------------------------------

    def _pull_registers(self):
        arr = (ctypes.c_uint32 * 17)()
        self._lib.nogba_get_arm9_regs(arr)
        self.arm9 = list(arr)[:16]
        self.cpsr9 = arr[16]
        self._lib.nogba_get_arm7_regs(arr)
        self.arm7 = list(arr)[:16]
        self.cpsr7 = arr[16]

    def _pull_framebuffers(self):
        buf_top = (ctypes.c_uint16 * (FB_W * FB_H))()
        buf_bot = (ctypes.c_uint16 * (FB_W * FB_H))()
        self._lib.nogba_get_framebuffer_top(buf_top)
        self._lib.nogba_get_framebuffer_bot(buf_bot)

        # Convert RGB565 to RGB888
        def convert(src):
            img = Image.new("RGB", (FB_W, FB_H))
            for i, pix in enumerate(src):
                r = ((pix >> 11) & 0x1F) << 3
                g = ((pix >> 5) & 0x3F) << 2
                b = (pix & 0x1F) << 3
                img.putpixel((i % FB_W, i // FB_W), (r, g, b))
            return img

        self.fb_top = convert(buf_top)
        self.fb_bot = convert(buf_bot)


class EmuDSNoGBA:
    """Tkinter GUI wrapper.  It is mostly unchanged but now consumes an
    *EmulatorCore* instance instead of generating random data directly."""

    def __init__(self, root, core: EmulatorCore | None = None):
        self.root = root
        self.core = core or NoGbaCore()
        self.rom_name = "NO GAME"
        self.running = False
        self._fonts()
        self._build_gui()
        self._draw_registers()
        self._draw_screens()
        self.status("Ready.")

    # --------------------------- UI plumbing -------------------------

    def _fonts(self):
        self.font = font.Font(family="MS Sans Serif", size=8)
        self.monofont = font.Font(family="Consolas", size=9)

    def _build_gui(self):
        menubar = tk.Menu(self.root, font=self.font)
        filem = tk.Menu(menubar, tearoff=0, font=self.font)
        filem.add_command(label="Load Game...", command=self.load_rom)
        filem.add_separator()
        filem.add_command(label="Exit", command=self.root.quit)
        menubar.add_cascade(label="File", menu=filem)

        emum = tk.Menu(menubar, tearoff=0, font=self.font)
        emum.add_command(label="Run", command=self.run)
        emum.add_command(label="Stop", command=self.stop)
        emum.add_command(label="Reset", command=self.reset)
        menubar.add_cascade(label="Emulation", menu=emum)

        tools = tk.Menu(menubar, tearoff=0, font=self.font)
        tools.add_command(label="Memory Viewer", command=self.memory_viewer)
        tools.add_command(label="VRAM Viewer", command=self.vram_viewer)
        tools.add_command(label="Sound Log", command=self.sound_log)
        menubar.add_cascade(label="Tools", menu=tools)

        self.root.config(menu=menubar)

        main = tk.Frame(self.root, bg="#B8B8B8")
        main.place(x=0, y=0, width=648, height=486)
        left = tk.Frame(main, bg="#B8B8B8")
        left.place(x=12, y=10, width=270, height=410)
        right = tk.Frame(main, bg="#B8B8B8")
        right.place(x=290, y=10, width=340, height=410)

        self.screen_top = tk.Label(left, bd=3, relief="ridge")
        self.screen_top.pack(pady=(2, 5))
        self.screen_bot = tk.Label(left, bd=3, relief="ridge")
        self.screen_bot.pack()

        tk.Label(right, text="CPU Registers (ARM9/ARM7)", font=self.font, bg="#B8B8B8").pack(anchor=tk.W, pady=(0, 2))
        self.reg_text = tk.Text(right, width=44, height=18, font=self.monofont, bg="#E7E7E7", bd=1, relief=tk.SUNKEN)
        self.reg_text.pack()
        self.reg_text.config(state=tk.DISABLED)

        self.mem_view = tk.Text(right, width=44, height=8, font=self.monofont, bg="#FFFBE7", bd=1, relief=tk.SUNKEN)
        self.mem_view.pack(pady=4)

        self.rom_label = tk.Label(self.root, text=f"ROM: {self.rom_name}", font=self.font, anchor=tk.W, bg="#F8F8F8", bd=1, relief=tk.SUNKEN)
        self.rom_label.place(x=0, y=462, width=480, height=24)
        self.statusbar = tk.Label(self.root, text="", font=self.font, anchor=tk.W, bg="#E0E0E0", bd=1, relief=tk.SUNKEN)
        self.statusbar.place(x=480, y=462, width=168, height=24)

    # --------------------------- Menu commands ----------------------

    def load_rom(self):
        fname = filedialog.askopenfilename(title="Select a Nintendo DS ROM", filetypes=[("NDS ROMs", "*.nds"), ("All files", "*.*")])
        if fname:
            if not self.core.load_rom(fname):
                self.status("Failed to load ROM in backend core.")
                return
            self.rom_name = os.path.basename(fname)
            self.rom_label.config(text=f"ROM: {self.rom_name}")
            self.status(f"Loaded {self.rom_name}")
            self.reset()
        else:
            self.status("ROM load cancelled.")

    def run(self):
        if self.rom_name == "NO GAME":
            self.status("No ROM loaded.")
            return
        if self.running:
            return
        self.running = True
        self.status("Emulation running...")
        self._animate()

    def stop(self):
        self.running = False
        self.status("Emulation stopped.")

    def reset(self):
        self.core.reset()
        self.running = False
        self._draw_registers()
        self._draw_memory_dump()
        self._draw_screens()
        self.status("System reset.")

    # --------------------------- Main loop --------------------------

    def _animate(self):
        if not self.running:
            return
        self.core.run_frame()
        self._draw_registers()
        self._draw_memory_dump()
        self._draw_screens()
        self.root.after(16, self._animate)  # ~60 fps

    # --------------------------- Rendering --------------------------

    def _draw_registers(self):
        arm9, arm7, cpsr9, cpsr7 = self.core.get_registers()
        text = "ARM9 (67 MHz)\n"
        for i in range(0, 16, 2):
            text += f"R{i:02}={arm9[i]:08X}   R{i+1:02}={arm9[i+1]:08X}\n"
        text += f"CPSR={cpsr9:08X}\n\nARM7 (33 MHz)\n"
        for i in range(0, 16, 2):
            text += f"R{i:02}={arm7[i]:08X}   R{i+1:02}={arm7[i+1]:08X}\n"
        text += f"CPSR={cpsr7:08X}\n"
        self.reg_text.config(state=tk.NORMAL)
        self.reg_text.delete("1.0", tk.END)
        self.reg_text.insert(tk.END, text)
        self.reg_text.config(state=tk.DISABLED)

    def _draw_memory_dump(self):
        dump = " ".join(f"{random.randint(0, 255):02X}" for _ in range(256))
        self.mem_view.config(state=tk.NORMAL)
        self.mem_view.delete("1.0", tk.END)
        self.mem_view.insert(tk.END, dump)
        self.mem_view.config(state=tk.DISABLED)

    def _draw_screens(self):
        fb_top, fb_bot = self.core.get_framebuffers()
        img_top = ImageTk.PhotoImage(fb_top.resize((256, 192)))
        img_bot = ImageTk.PhotoImage(fb_bot.resize((256, 192)))
        # Keep a reference so Tk doesn’t GC the images.
        self.screen_top.img = img_top
        self.screen_bot.img = img_bot
        self.screen_top.configure(image=img_top)
        self.screen_bot.configure(image=img_bot)

    # --------------------------- Status & stubs ---------------------

    def status(self, msg):
        self.statusbar.config(text=msg)

    def memory_viewer(self):
        self.status("Memory viewer not implemented (placeholder)")

    def vram_viewer(self):
        self.status("VRAM viewer not implemented (placeholder)")

    def sound_log(self):
        self.status("Sound log not implemented (placeholder)")


if __name__ == "__main__":
    root = tk.Tk()
    root.geometry("648x486")
    root.title("emuds — NO$GBA style (Python)")
    root.resizable(False, False)

    # Prefer real no$gba backend when available
    try:
        core = NoGbaCore()
        if isinstance(core, NoGbaCore) and core._lib:
            print("[INFO] no$gba core successfully loaded.")
    except Exception as e:
        print(f"[ERROR] Failed to initialize no$gba core: {e}")
        print("[INFO] Falling back to dummy core.")
        core = EmulatorCore()

    EmuDSNoGBA(root, core)
    root.mainloop()
