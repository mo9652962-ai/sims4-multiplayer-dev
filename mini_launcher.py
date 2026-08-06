# -*- coding: utf-8 -*-
"""最简 CustomTkinter 测试（诊断 exe 打包问题）"""
import customtkinter as ctk
from PIL import Image, ImageTk

ctk.set_appearance_mode("dark")
app = ctk.CTk()
app.title("Mini Test")
app.geometry("300x200")
ctk.CTkLabel(app, text="Hello").pack(pady=20)
ctk.CTkButton(app, text="OK").pack()
app.mainloop()
