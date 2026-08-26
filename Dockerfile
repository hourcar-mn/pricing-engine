# check=skip=FromPlatformFlagConstDisallowed

# Backend container for Cloudflare Containers. Runs the same app.py server as
# local dev -- the only differences are the bind address and where the data
# files come from (R2, see r2_sync.py).
#
# The platform pin is deliberate and the lint for it is skipped above:
# Cloudflare only runs linux/amd64 images, so building for the host's
# architecture on an ARM machine would produce something that can't be deployed.
FROM --platform=linux/amd64 python:3.13-slim

WORKDIR /app

COPY requirements.txt .
# numpy/pandas/scipy are most of this image and none of it is optional, but the
# bundled test suites are ~150MB of pure ballast.
RUN pip install --no-cache-dir -r requirements.txt \
    && find /usr/local/lib/python3.13/site-packages \
        -type d -name tests -prune -exec rm -rf {} +

COPY . .

# app.py reads both of these; the Worker connects on 8000.
ENV HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
# main() defaults to open_browser=False, which is what we want here
CMD ["python", "-c", "from app import main; main()"]
