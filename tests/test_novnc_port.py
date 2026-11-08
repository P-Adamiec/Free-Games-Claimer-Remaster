"""noVNC listens on NOVNC_PORT inside the container (start-vnc.sh), so the port mapping and the health checks use it too."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_compose_file_maps_the_same_port_on_both_sides():
    # Before 1.19 it mapped NOVNC_PORT to 7080, while websockify listened on NOVNC_PORT: any other port cut noVNC off.
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"${NOVNC_PORT:-7080}:${NOVNC_PORT:-7080}"' in compose
    assert "curl --fail http://localhost:$${NOVNC_PORT:-7080}" in compose


def test_the_image_health_check_follows_the_port():
    assert 'curl --fail "http://localhost:${NOVNC_PORT:-7080}"' in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_novnc_itself_listens_on_that_port():
    script = (ROOT / "start-vnc.sh").read_text(encoding="utf-8")
    assert 'NOVNC_PORT="${NOVNC_PORT:-7080}"' in script and 'websockify -D --web "/usr/share/novnc/" "$NOVNC_PORT"' in script
