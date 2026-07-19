"""
A small always-on-top subtitle window (built-in tkinter, no extra install) that shows
the current line of dialogue as it's spoken during a --live-audio pipeline run - like
closed captions, so someone can read along.

Tkinter must be driven from the main thread. This works because live_audio.speak_turn()
already runs synchronously in the main thread (generate audio, show subtitle, play,
return) - SubtitleWindow.show() calls root.update() once to force an immediate
repaint, rather than needing a separate GUI thread or a persistent mainloop() elsewhere.
"""

import tkinter as tk


ROLE_COLORS = {
    "ESTIMATOR": "#4fc3f7",
    "CALLER": "#4fc3f7",
    "CLOSER": "#4fc3f7",
    "CUSTOMER": "#ffca28",
    "COUNTERPARTY": "#ffca28",
}
DEFAULT_ROLE_COLOR = "#ffffff"


class SubtitleWindow:
    def __init__(self, width: int = 900, height: int = 220):
        self.root = tk.Tk()
        self.root.title("The Negotiator - Live Subtitles")
        self.root.configure(bg="#111111")
        self.root.attributes("-topmost", True)
        self.root.geometry(f"{width}x{height}")

        self.role_label = tk.Label(
            self.root, text="", fg=DEFAULT_ROLE_COLOR, bg="#111111",
            font=("Helvetica", 16, "bold"),
        )
        self.role_label.pack(pady=(20, 4))

        self.text_label = tk.Label(
            self.root, text="Waiting for the conversation to start...", fg="#eeeeee", bg="#111111",
            font=("Helvetica", 20), wraplength=width - 60, justify="center",
        )
        self.text_label.pack(padx=30, pady=(0, 20), expand=True)

        self.root.update()

    def show(self, role: str, text: str):
        color = ROLE_COLORS.get(role, DEFAULT_ROLE_COLOR)
        self.role_label.config(text=role, fg=color)
        self.text_label.config(text=text)
        self.root.update()

    def close(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass  # already closed (e.g. user clicked the window shut)
