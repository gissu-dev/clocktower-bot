# Install Clocktower Bot Start Menu Launcher

This guide adds a Start Menu shortcut so you can launch the bot from Windows Search.

## 1) Open the Start Menu Programs folder

1. Press `Win + R`.
2. Type `shell:programs` and press Enter.

This opens your user Start Menu shortcuts folder.

## 2) Create the start shortcut

1. In the opened folder, right-click and choose **New > Shortcut**.
2. For the shortcut target, browse to and select:
   - `start_clocktower.vbs`
3. Click **Next**.
4. Name it exactly:
   - `Clocktower Bot`
5. Click **Finish**.

## 3) Use Windows Search

1. Press the Windows key.
2. Type `Clocktower Bot` to start.
3. To stop the bot, close the command window (or end the Python process in Task Manager).

## 4) If Search does not update immediately

Restart Windows Explorer:

1. Press `Ctrl + Shift + Esc` to open Task Manager.
2. Find **Windows Explorer** in the process list.
3. Right-click it and choose **Restart**.
4. Try Windows Search again.
