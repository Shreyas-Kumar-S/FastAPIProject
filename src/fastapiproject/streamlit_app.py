import html
import time

import streamlit as st

from fastapiproject import api_client

st.set_page_config(page_title="RAG PDF Assistant", page_icon="📄", layout="wide")

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap');

    :root {
        --paper: #E9ECE7;
        --surface: #FFFFFF;
        --ink: #1B2420;
        --ink-muted: #5B6560;
        --accent: #1F5C52;
        --citation: #8C3B2E;
        --rule: #C7CFC9;
    }

    html, body, [class*="css"] {
        font-family: 'IBM Plex Sans', sans-serif;
        color: var(--ink);
    }

    .stApp {
        background-color: var(--paper);
    }

    .block-container {
        max-width: 880px;
        padding-top: 2.5rem;
    }

    h1, h2, h3 {
        font-family: 'Source Serif 4', serif;
        color: var(--ink);
    }

    .stMarkdown p {
        font-family: 'Source Serif 4', serif;
    }

    .stButton > button {
        background-color: var(--accent);
        color: var(--surface);
        border: none;
        border-radius: 4px;
        font-family: 'IBM Plex Sans', sans-serif;
        font-weight: 500;
    }

    .stButton > button:hover {
        background-color: #164941;
        color: var(--surface);
    }

    *:focus-visible {
        outline: 2px solid var(--accent);
        outline-offset: 2px;
    }

    .stTabs [aria-selected="true"] {
        color: var(--accent) !important;
        border-bottom-color: var(--accent) !important;
    }

    .stTabs [data-baseweb="tab"] {
        font-family: 'IBM Plex Sans', sans-serif;
    }

    .citation-chip-row {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
        margin-top: 0.75rem;
    }

    .citation-chip {
        background-color: var(--surface);
        border: 1px solid var(--rule);
        border-left: 3px solid var(--citation);
        border-radius: 3px;
        padding: 0.35rem 0.6rem;
        font-family: 'IBM Plex Mono', monospace;
        font-size: 0.85rem;
        color: var(--ink-muted);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("RAG PDF Assistant")


def render_citation_chips(sources: list[str]) -> None:
    if not sources:
        return
    chips = "".join(f'<span class="citation-chip">Source: {html.escape(s)}</span>' for s in sources)
    st.markdown(f'<div class="citation-chip-row">{chips}</div>', unsafe_allow_html=True)


ask_tab, upload_tab = st.tabs(["Ask a Question", "Upload PDFs"])

with ask_tab:
    question = st.text_input("Question")

    with st.popover("⚙️ Settings"):
        top_k = st.slider("Top K", 1, 20, value=5)
        score_threshold = st.slider("Score threshold", 0.0, 1.0, value=0.5, step=0.05)

    if st.button("Ask", key="ask_button") and question:
        try:
            result = api_client.ask(question, top_k=top_k, score_threshold=score_threshold)
        except api_client.ApiError as exc:
            st.error(exc.detail)
        else:
            st.write(result.answers)
            render_citation_chips(result.sources)

with upload_tab:
    uploaded_files = st.file_uploader("PDFs", accept_multiple_files=True, type=["pdf"])

    if st.button("Upload", key="upload_button") and uploaded_files:
        files = [(f.name, f.getvalue()) for f in uploaded_files]
        try:
            ingest_result = api_client.ingest(files)
        except api_client.ApiError as exc:
            st.error(exc.detail)
        else:
            st.session_state["ingest_event_id"] = ingest_result.event_id
            st.session_state["ingest_status"] = None
            st.session_state["ingest_poll_requested"] = True

    event_id = st.session_state.get("ingest_event_id")
    if event_id:
        st.markdown(f"Event ID: `{event_id}`")

        status = st.session_state.get("ingest_status")

        if st.session_state.get("ingest_poll_requested"):
            st.session_state["ingest_poll_requested"] = False
            with st.spinner("Ingesting..."):
                for _ in range(20):
                    try:
                        status = api_client.check_status(event_id)
                    except api_client.ApiError as exc:
                        st.error(exc.detail)
                        status = None
                        break
                    if status.status in ("Completed", "Failed"):
                        break
                    time.sleep(1.5)
            st.session_state["ingest_status"] = status

        if status is not None:
            if status.status == "Completed":
                if status.ingested is not None:
                    st.success(f"Ingested {status.ingested} chunks.")
                else:
                    st.success("Upload complete.")
            elif status.status == "Failed":
                st.error(status.error or "Ingestion failed (no details returned).")
            else:
                st.info(f"Still {status.status.lower()}...")
                if st.button("Check again", key="check_again_button"):
                    st.session_state["ingest_poll_requested"] = True
                    st.rerun()
