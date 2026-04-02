import asyncio
from typing import Any, Dict

import streamlit as st

from client.client import Client


def _run_async(coro):
    return asyncio.run(coro)


async def _perform_request(
    host: str,
    port: int,
    client_id: str,
    action: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    client = Client(host=host, port=port, client_id=client_id)
    await client.connect()
    try:
        if action == "SEARCH":
            return await client.search(payload["train"])
        if action == "TIER_SEATS":
            return await client.tier_available_seats(payload["train"], payload["tier"])
        if action == "BOOK":
            return await client.book_tier(payload["train"], payload["tier"])
        if action == "BOOK_TIER_SEAT":
            return await client.book_tier_seat(payload["train"], payload["tier"], payload["seat"])
        if action == "CANCEL":
            return await client.cancel_ticket(payload["ticket_id"])
        if action == "STATUS":
            return await client.ticket_status(payload["ticket_id"])
        raise ValueError(f"unsupported action: {action}")
    finally:
        await client.close()


def _show_response(resp: Dict[str, Any]) -> None:
    if resp.get("status") != "OK":
        code = resp.get("code", "ERROR")
        message = resp.get("message", "Request failed")
        st.error(f"{code}: {message}")
        return

    data = resp.get("data")
    if not isinstance(data, dict):
        st.warning("No response data returned.")
        return

    if "tiers" in data and isinstance(data["tiers"], list):
        st.success("Availability fetched.")
        st.subheader("Tier Availability")
        st.dataframe(data["tiers"], width="stretch")
    elif "ticket" in data and isinstance(data["ticket"], dict):
        ticket = data["ticket"]
        ticket_status = data.get("ticket_status", ticket.get("status", "unknown"))
        if ticket_status == "confirmed":
            st.success(f"Ticket confirmed: {ticket.get('ticket_id', '-')}")
        elif ticket_status == "waiting":
            pos = data.get("waiting_position")
            if pos is not None:
                st.info(f"Added to waiting list at position {pos}: {ticket.get('ticket_id', '-')}")
            else:
                st.info(f"Added to waiting list: {ticket.get('ticket_id', '-')}")
        else:
            st.info(f"Ticket created: {ticket.get('ticket_id', '-')}")
        st.dataframe([ticket], width="stretch")
    elif "cancelled_ticket" in data:
        st.success("Ticket cancelled.")
        st.subheader("Cancelled Ticket")
        st.dataframe([data["cancelled_ticket"]], width="stretch")
        promoted = data.get("promoted_ticket")
        if isinstance(promoted, dict):
            st.subheader("Promoted From Waiting List")
            st.dataframe([promoted], width="stretch")
    elif {"ticket_id", "status", "train_id", "tier"}.issubset(set(data.keys())):
        st.success("Status fetched.")
        st.dataframe([data], width="stretch")
    else:
        st.info("Request completed.")
        st.dataframe([data], width="stretch")


def main() -> None:
    st.set_page_config(page_title="Train Reservation UI", page_icon=":seat:", layout="wide")
    st.title("Distributed Train Reservation")
    st.caption("Search availability, book by tier, cancel tickets, and check ticket status.")

    with st.sidebar:
        st.header("Connection")
        host = st.text_input("Host", value="127.0.0.1")
        port = st.number_input("Port", min_value=1, max_value=65535, value=9000, step=1)
        client_id = st.text_input("Client ID", value="streamlit-user")

    tab_search, tab_book, tab_cancel, tab_status = st.tabs(
        ["Search", "Book", "Cancel", "Ticket Status"]
    )

    with tab_search:
        st.write("Check seat availability and waiting list lengths by tier.")
        search_train = st.text_input("Train ID", value="T1", key="search_train")
        if st.button("Search", key="search_btn", width="stretch"):
            try:
                resp = _run_async(
                    _perform_request(
                        host=host,
                        port=int(port),
                        client_id=client_id,
                        action="SEARCH",
                        payload={"train": search_train.strip()},
                    )
                )
                _show_response(resp)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Request failed: {exc}")

    with tab_book:
        st.write("Book by tier with optional seat-number selection for lock demo.")
        book_train = st.text_input("Train ID", value="T1", key="book_train")
        tier = st.selectbox("Tier", options=["1AC", "2AC", "3AC"], index=2)

        available_seat_numbers: list[int] = []
        if book_train.strip():
            try:
                seats_resp = _run_async(
                    _perform_request(
                        host=host,
                        port=int(port),
                        client_id=client_id,
                        action="TIER_SEATS",
                        payload={"train": book_train.strip(), "tier": tier},
                    )
                )
                if seats_resp.get("status") == "OK":
                    seat_data = seats_resp.get("data", {})
                    if isinstance(seat_data, dict):
                        available_seat_numbers = seat_data.get("available_seat_numbers", [])
                else:
                    st.error(seats_resp.get("message", "Failed to fetch available seats."))
            except Exception as exc:  # noqa: BLE001
                st.error(f"Failed to fetch available seats: {exc}")

        seat_options = ["Auto Allocation"] + [str(n) for n in available_seat_numbers]
        selected_seat = st.selectbox("Seat Number", options=seat_options, key="book_seat_num")

        if selected_seat == "Auto Allocation":
            payload = {"train": book_train.strip(), "tier": tier}
            action = "BOOK"
        else:
            payload = {"train": book_train.strip(), "tier": tier, "seat": int(selected_seat)}
            action = "BOOK_TIER_SEAT"
            st.caption("For lock demo: open two tabs with different Client ID values and select the same train, tier, and seat number.")

        if available_seat_numbers:
            st.caption(f"Available seat numbers in {tier}: {len(available_seat_numbers)}")
        else:
            st.caption(f"No available seat numbers in {tier}. You can still use Auto Allocation to join waiting list.")

        if st.button("Book Ticket", key="book_btn", width="stretch"):
            try:
                resp = _run_async(
                    _perform_request(
                        host=host,
                        port=int(port),
                        client_id=client_id,
                        action=action,
                        payload=payload,
                    )
                )
                _show_response(resp)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Request failed: {exc}")

    with tab_cancel:
        st.write("Cancel a ticket. Confirmed tickets may trigger waiting-list promotion.")
        cancel_ticket_id = st.text_input("Ticket ID", key="cancel_ticket_id")
        if st.button("Cancel Ticket", key="cancel_btn", width="stretch"):
            if not cancel_ticket_id.strip():
                st.warning("Enter a ticket ID.")
            else:
                try:
                    resp = _run_async(
                        _perform_request(
                            host=host,
                            port=int(port),
                            client_id=client_id,
                            action="CANCEL",
                            payload={"ticket_id": cancel_ticket_id.strip()},
                        )
                    )
                    _show_response(resp)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Request failed: {exc}")

    with tab_status:
        st.write("Check the current status of a ticket.")
        status_ticket_id = st.text_input("Ticket ID", key="status_ticket_id")
        if st.button("Get Status", key="status_btn", width="stretch"):
            if not status_ticket_id.strip():
                st.warning("Enter a ticket ID.")
            else:
                try:
                    resp = _run_async(
                        _perform_request(
                            host=host,
                            port=int(port),
                            client_id=client_id,
                            action="STATUS",
                            payload={"ticket_id": status_ticket_id.strip()},
                        )
                    )
                    _show_response(resp)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Request failed: {exc}")

    st.divider()
    st.caption("Results are shown in a clean format.")


if __name__ == "__main__":
    main()
