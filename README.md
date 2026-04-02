# Distributed Reservation System

## Run

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Start the server:

```bash
python -m server.server --host 127.0.0.1 --port 9000
```

3. Launch Streamlit UI:

```bash
streamlit run streamlit_app.py
```

The UI supports:
- `SEARCH` by train
- `BOOK` by tier (`1AC`, `2AC`, `3AC`)
- `CANCEL` by `ticket_id`
- `STATUS` by `ticket_id`
