# Beginner's Guide for Windows (Zero to Hero)

If you have **never used Docker before**, navigating command lines can be intimidating. This exact step-by-step guide is structured for absolute beginners who just want the Free Games Claimer to run quietly in the background on Windows upon computer startup.

---

## Phase 1: Installing Docker Desktop 🐋
Docker is the engine that will run the project safely in a virtual container without messing with your Windows files.

1. Download **Docker Desktop** for Windows from the [official website](https://www.docker.com/products/docker-desktop/).
2. Run the installer. Ensure that the option to use **WSL 2 (Windows Subsystem for Linux)** is left **checked**.
3. After installation (it may require a PC restart), open Docker Desktop.
4. **Crucial setup:** Click the "Gear" icon (Settings) in the top right. Under the "General" tab, ensure that **"Start Docker Desktop when you log in"** is checked. This guarantees it wakes up with your Windows.

---

## Phase 2: Optimizing WSL Resources (Extremely Important!) 💾
By default, Docker's WSL 2 engine can greedily consume a lot of your system's RAM. It must be strictly limited to a totally safe **3GB**, ensuring your gaming PC never feels slow while running it in the background.

1. Press `Win + R` on your keyboard, type `%USERPROFILE%` and hit **Enter**. (This opens your main User folder like `C:\Users\YourName`).
2. **Crucial:** Windows hides file extensions by default. In File Explorer, click "View" at the top -> "Show" -> check "File name extensions".
3. Right-click in the empty space -> New -> Text Document.
4. Name it exactly **`.wslconfig`** and delete the `.txt` part. (When asked if you want to change the extension, click Yes).
5. Open this file with Notepad and copy/paste the following:
```ini
[wsl2]
memory=3GB
```
5. Save the file.
6. Open your Windows **Command Prompt** (`cmd`) and type `wsl --shutdown` and press enter. When Docker turns back on, it will strictly obey the 3GB limit!

---

## Phase 3: Getting Dockhand (The Easy Visual Interface) 🌐
Instead of interacting with black terminal windows, installing **Dockhand** is highly recommended. It provides a beautiful visual web interface that lets you see your container, edit passwords, and press "Start/Restart" buttons easily without knowing terminal commands.

To install Dockhand, follow the official quick start deployment guides at the **[Dockhand Website](https://dockhand.pro)** or simply deploy its standard docker container. Create your admin account when it's up and running!

---

## Phase 4: Deploying Free Games Claimer

There are two primary ways to set up the claimer. **Method A** is highly recommended if you use Dockhand!

### Method A: Using Dockhand (Visual Web UI)
1. Inside your Dockhand dashboard, navigate to the **Stacks** or **Compose** section.
2. Name the stack however you like, for example: `free-games-claimer`.
3. In the text editor box, **paste the entire contents of the `docker-compose.yml` file** from the GitHub repository.
4. Set up your `.env` configuration (e.g. `EG_EMAIL=twoj@mail.com`) within the environment variables block or `.env` editor in Dockhand matching the `.env.example` file.
5. Click **Deploy / Update**. That's it! Dockhand handles the downloading and execution automatically in the background.

51: ### Method B: The Classic Folder Method
If for some reason you don't want to use a visual interface:
1. Go to the main page of this GitHub repository. Click the green **"Code"** button and select **"Download ZIP"**.
2. Extract the ZIP file somewhere safe (like `Documents/free-games-claimer`).
3. Inside the extracted folder, find `.env.example`, rename it strictly to `.env`, and fill out your logins inside it using Notepad.
4. **Open Terminal exactly in this folder:** Click on the address bar at the very top of the window, erase the text, type `cmd`, and hit **Enter**. A black command window will pop up already pointing to your exact folder!
5. In that black window, type `docker compose up -d` and hit Enter. The `-d` explicitly tells Docker to run it detached (invisibly).

---

---

## Phase 5: The First-Time Login (Optional) 🔐
If you provided your emails and passwords safely in the `.env` file, the bot will try to log in entirely by itself! However, **if you left them blank** or **if stores require a 2FA code (like Steam Guard mobile tokens or email verification)**, the system will pause and wait for your manual intervention.

1. Open your internet browser and go to **`http://localhost:7080`**.
2. You will see a live video feed of the bot operating a virtual Chrome browser inside the container.
3. If it is stuck at a login screen waiting for a 2FA code, password, or CAPTCHA, simply interact with the window and log in normally using your mouse/keyboard. 
4. Once you see the storefront's homepage, **you can simply close the `7080` tab**.

From now on, because your container is configured with `restart: unless-stopped`, the Free Games Claimer operates autonomously utilizing its built-in memory of your session. It will silently wake up, check for free games every 12 hours behind the scenes, and go back to sleep. 

Enjoy the automated free games!
