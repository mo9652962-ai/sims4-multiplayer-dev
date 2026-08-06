# -*- coding: utf-8 -*-
"""逐步打印版 mini（定位 exe 打包卡点）"""
import sys

def log(msg):
    with open(r'C:\Users\31954\AppData\Local\Temp\mini2.log', 'a', encoding='utf-8') as f:
        f.write(msg + '\n')

log('1. start')
try:
    log('2. import customtkinter...')
    import customtkinter as ctk
    log('3. ctk imported: ' + str(ctk.__version__ if hasattr(ctk, '__version__') else '?'))
except Exception as e:
    log('3. FAIL ctk: ' + repr(e))
    raise

try:
    log('4. import PIL...')
    from PIL import Image, ImageTk
    log('5. PIL imported')
except Exception as e:
    log('5. FAIL PIL: ' + repr(e))
    raise

try:
    log('6. set appearance...')
    ctk.set_appearance_mode("dark")
    log('7. create CTk...')
    app = ctk.CTk()
    log('8. CTk created')
    app.title("Mini Test")
    app.geometry("300x200")
    ctk.CTkLabel(app, text="Hello").pack(pady=20)
    ctk.CTkButton(app, text="OK").pack()
    log('9. entering mainloop...')
    app.mainloop()
    log('10. mainloop exited')
except Exception as e:
    log('ERR: ' + repr(e))
    raise
