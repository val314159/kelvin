import subprocess


def bounce_sandbox(path: str):
    """Restart sandbox with new path."""
    print("rm old sandbox")
    result = subprocess.run(
        ["docker", "rm", "-f", "sandbox"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    print("launch new sandbox")
    gitp = f"{path}/.git"
    result = subprocess.run(
        ["docker", "run", "-d", "--rm",
         "-v", f"{path}:{path}", "-w", path,
         "-v", f"{gitp}:{gitp}:ro",
         "--name", "sandbox", "sandbox",
         "sleep", "5000000000"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    print("new sandbox up!")
    return result
