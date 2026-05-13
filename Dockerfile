# ──────────────────────────────────────────────────────────────
# Free Games Claimer Remaster – Docker image
#
# This file tells Docker how to build the container that runs
# the bot.  It installs Python, a virtual display (so Chrome
# can run without a real monitor), and a web-based VNC viewer
# so you can watch the browser remotely.
# ──────────────────────────────────────────────────────────────

# Start from Debian Bookworm (a lightweight Linux distribution).
# Using Debian instead of Alpine because Chrome needs glibc.
FROM debian:bookworm-slim

# Use bash as the shell (instead of sh) for better scripting support
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Prevent interactive prompts during package installation
ARG DEBIAN_FRONTEND=noninteractive

# ── Install all system dependencies in a single RUN to reduce image layers ──
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        # Core tools: curl for downloads, ca-certificates for HTTPS, gnupg for GPG keys
        curl ca-certificates gnupg \
        # Python and pip (the bot is written in Python)
        python3 python3-pip \
        # dos2unix fixes Windows line endings; tini is a proper init process for Docker
        dos2unix tini \
    && mkdir -p /etc/apt/keyrings \
    # ── Install TurboVNC & VirtualGL (virtual display system) ──
    # These let Chrome render pages even without a real monitor
    && curl --proto "=https" --tlsv1.2 -fsSL https://packagecloud.io/dcommander/virtualgl/gpgkey | gpg --dearmor -o /etc/apt/trusted.gpg.d/VirtualGL.gpg \
    && curl --proto "=https" --tlsv1.2 -fsSL https://packagecloud.io/dcommander/turbovnc/gpgkey | gpg --dearmor -o /etc/apt/trusted.gpg.d/TurboVNC.gpg \
    && curl --proto "=https" --tlsv1.2 -fsSL https://raw.githubusercontent.com/VirtualGL/repo/main/VirtualGL.list > /etc/apt/sources.list.d/VirtualGL.list \
    && curl --proto "=https" --tlsv1.2 -fsSL https://raw.githubusercontent.com/TurboVNC/repo/main/TurboVNC.list > /etc/apt/sources.list.d/TurboVNC.list \
    && apt-get update \
    && apt-get install --no-install-recommends -y \
        # VirtualGL + TurboVNC = virtual display; ratpoison = lightweight window manager
        virtualgl turbovnc ratpoison \
        # noVNC + websockify = VNC viewer accessible through a web browser
        novnc websockify \
    # ── Shared libraries required by Chrome/Chromium ──
    && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 \
        libxfixes3 libxdamage1 \
    # ── Install browser: Google Chrome on x86/x64, Chromium on ARM (Raspberry Pi) ──
    && ARCH=$(dpkg --print-architecture) \
    && if [ "$ARCH" = "amd64" ]; then \
           curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
           && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
           && apt-get update \
           && apt-get install --no-install-recommends -y google-chrome-stable; \
       else \
           apt-get install --no-install-recommends -y chromium; \
       fi \
    # Clean up package manager cache to reduce image size
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/* /var/tmp/* /tmp/* /usr/share/doc/* \
    # Create a convenient noVNC landing page
    && ln -s /usr/share/novnc/vnc_auto.html /usr/share/novnc/index.html \
    # Make "python" command available (Debian only has "python3" by default)
    && ln -sf /usr/bin/python3 /usr/bin/python

# Set the working directory inside the container
WORKDIR /fgc

# ── Install Python libraries (like nodriver, sqlalchemy, etc.) ──
COPY requirements.txt ./
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# ── Copy the application source code into the container ──
COPY . .

# Fix Windows-style line endings (\r\n → \n) in shell scripts and make them executable
RUN dos2unix ./*.sh && chmod +x ./*.sh
RUN cp docker-entrypoint.sh /usr/local/bin/

# ── Build information (injected by CI/CD during docker build) ──
# These variables are baked into the image so the startup banner can show
# which commit and branch the image was built from.
ARG COMMIT=""
ARG BRANCH=""
ARG NOW=""
ENV COMMIT=${COMMIT}
ENV BRANCH=${BRANCH}
ENV NOW=${NOW}

# ── VNC settings (remote desktop access via web browser) ──
ENV VNC_PORT=5900
ENV NOVNC_PORT=7080
EXPOSE 7080

# ── Display settings (virtual screen resolution for the browser) ──
ENV WIDTH=1280
ENV HEIGHT=720
ENV DEPTH=24
ENV SHOW=1

# ── Health check: Docker uses this to know if the container is still working ──
# It checks that python3 is running AND that the noVNC web server is responding
HEALTHCHECK --interval=10s --timeout=5s CMD pgrep python3 && curl --fail http://localhost:7080 || exit 1

# ── Container startup: run the entrypoint script first, then start the bot ──
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "main.py"]
