FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir requests
COPY . .

# Exit when either collector dies so Fly restarts the machine.
CMD ["bash", "-c", "python vehicle_heading_collector.py & python combined_stop_visit_tracker.py & wait -n"]
