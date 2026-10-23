# The extract image: Python for the tools, Erlang and gcc for EGS's quest compiler (egs-build.sh).
#   docker build -t psu-extract extract
#   docker run --rm -v /path/to/discs:/discs:ro -v /path/to/data:/data -v psu-extract-cache:/work psu-extract \
#     --psu "/discs/Phantasy Star Universe (USA).iso" --aoti "/discs/<AotI>.iso" --out /data
# EGS is downloaded from GitHub on the first run (into /work), so the container needs network access once.
FROM erlang:26
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PSU_EXTRACT_WORK=/work
WORKDIR /opt
COPY . /opt/extract/
VOLUME ["/work"]
ENTRYPOINT ["python3", "-m", "extract", "--work", "/work"]
