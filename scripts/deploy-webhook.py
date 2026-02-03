#!/usr/bin/env python3
"""
deploy-webhook.py - Simple webhook receiver for triggering AutoMem deployments

Listens for POST requests and triggers the sync-and-deploy script.
Can be triggered by GitHub Actions or manually.

Usage:
    python scripts/deploy-webhook.py

Environment:
    WEBHOOK_PORT    - Port to listen on (default: 9000)
    WEBHOOK_SECRET  - Secret for validating requests
    AUTOMEM_DIR     - AutoMem directory
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "9000"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "automem-deploy-secret")
AUTOMEM_DIR = os.getenv("AUTOMEM_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_FILE = "/var/log/automem-webhook.log"


def log(message):
    """Log message with timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(log_line + "\n")
    except:
        pass


def verify_signature(payload, signature, secret):
    """Verify webhook signature."""
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/deploy":
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(content_length)

        # Verify signature
        signature = self.headers.get("X-Webhook-Secret") or self.headers.get("X-Hub-Signature-256")
        if WEBHOOK_SECRET and not verify_signature(payload, signature, WEBHOOK_SECRET):
            # Also accept plain secret in header
            if self.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
                log(f"Invalid signature from {self.client_address[0]}")
                self.send_error(403, "Invalid signature")
                return

        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            data = {}

        action = data.get("action", "deploy")
        source = data.get("source", "unknown")

        log(f"Received {action} request from {source} ({self.client_address[0]})")

        if action == "deploy":
            # Run sync-and-deploy in auto mode
            script_path = os.path.join(AUTOMEM_DIR, "scripts", "sync-and-deploy.sh")

            if not os.path.exists(script_path):
                log(f"Script not found: {script_path}")
                self.send_error(500, "Deploy script not found")
                return

            try:
                log("Starting deployment...")
                result = subprocess.run(
                    [script_path, "--auto"],
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout
                    cwd=AUTOMEM_DIR,
                )

                if result.returncode == 0:
                    log(f"Deployment successful:\n{result.stdout}")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps(
                            {
                                "status": "success",
                                "message": "Deployment completed",
                                "output": result.stdout[-1000:],  # Last 1000 chars
                            }
                        ).encode()
                    )
                else:
                    log(f"Deployment failed:\n{result.stderr}")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps(
                            {
                                "status": "error",
                                "message": "Deployment failed",
                                "error": result.stderr[-1000:],
                            }
                        ).encode()
                    )

            except subprocess.TimeoutExpired:
                log("Deployment timed out")
                self.send_error(504, "Deployment timed out")
            except Exception as e:
                log(f"Deployment error: {e}")
                self.send_error(500, str(e))

        elif action == "check":
            # Just check for updates without deploying
            script_path = os.path.join(AUTOMEM_DIR, "scripts", "sync-and-deploy.sh")
            result = subprocess.run(
                [script_path, "--check"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=AUTOMEM_DIR,
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "output": result.stdout}).encode())

        else:
            self.send_error(400, f"Unknown action: {action}")

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"status": "healthy", "service": "automem-webhook"}).encode()
            )
        else:
            self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        """Override to use our logging."""
        log(f"HTTP: {args[0]}")


def main():
    log(f"Starting webhook server on port {WEBHOOK_PORT}")
    log(f"AutoMem directory: {AUTOMEM_DIR}")

    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), WebhookHandler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
