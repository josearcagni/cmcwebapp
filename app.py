# app.py
import streamlit as st
from PIL import Image
import pandas as pd
import yaml
from yaml.loader import SafeLoader
import streamlit_authenticator as stauth
import plotly.express as px
from datetime import datetime, date
import smtplib
from email.message import EmailMessage
from dateutil.relativedelta import relativedelta
import os
import re

# ---------------------------
# Config / constants
# ---------------------------
EXCEL_PATH = "DB-CMC2.xlsx"
FIXED_EXPIRY_YEARS = 4  # fallback if needed
STATUS_OPTIONS = ["In use", "In stock", "Out of use"]

# ---------------------------
# Branding and page config (must call set_page_config early)
# ---------------------------
try:
    logo = Image.open("cmc_logo_white.png")
    st.set_page_config(page_title="CMC Pumps", page_icon=logo, layout="wide")
    st.image(logo, width=200)
except Exception:
    st.set_page_config(page_title="CMC Pumps", layout="wide")

st.title("CMC Pharma Solutions Traceability & Compliance Tool")

# ---------------------------
# Helpers
# ---------------------------
def is_empty(val):
    try:
        if pd.isna(val):
            return True
    except Exception:
        pass
    try:
        return str(val).strip() == ""
    except Exception:
        return False

def parse_date_from_id(id_value):
    if is_empty(id_value):
        return pd.NaT
    try:
        parts = re.split(r"\s-\s", str(id_value).strip())
        date_candidate = parts[-1].strip()
        parsed = pd.to_datetime(date_candidate, dayfirst=True, errors="coerce")
        if pd.notna(parsed):
            return parsed
        parsed = pd.to_datetime(date_candidate, dayfirst=False, errors="coerce")
        if pd.notna(parsed):
            return parsed
    except Exception:
        pass
    return pd.NaT

# ---------------------------
# Email sending (silent failures)
# ---------------------------
def send_email(to_email, subject, body):
    EMAIL = st.secrets.get("email_user") if isinstance(st.secrets, dict) else st.secrets.get("email_user", None)
    PASS = st.secrets.get("email_pass") if isinstance(st.secrets, dict) else st.secrets.get("email_pass", None)
    if not EMAIL or not PASS:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL
    msg["To"] = to_email
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(EMAIL, PASS)
            smtp.send_message(msg)
            st.success(f"✅ Email sent to {to_email}")
            return True
    except Exception:
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as smtp:
                smtp.starttls()
                smtp.login(EMAIL, PASS)
                smtp.send_message(msg)
                st.success(f"✅ Email sent to {to_email}")
                return True
        except Exception:
            return False

# ---------------------------
# DB load/save
# ---------------------------
@st.cache_data(ttl=60)
def load_db():
    if not os.path.exists(EXCEL_PATH):
        cols = ["ID", "Client", "Model", "Quantity Sold", "Serial Number", "Year", "Status",
                "Last Updated", "Expiry", "Patient", "Notes"]
        df_empty = pd.DataFrame(columns=cols)
        df_empty.to_excel(EXCEL_PATH, index=False)
    try:
        df = pd.read_excel(EXCEL_PATH, engine="openpyxl")
    except Exception:
        df = pd.read_excel(EXCEL_PATH)
    df.columns = df.columns.str.strip()
    if "Last Updated" not in df.columns:
        df["Last Updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if "Expiry" not in df.columns:
        df["Expiry"] = pd.NaT
    if "Patient" not in df.columns:
        df["Patient"] = pd.NA
    if "Quantity Sold" not in df.columns:
        df["Quantity Sold"] = 0
    if "Notes" not in df.columns:
        df["Notes"] = ""
    df["Status"] = df.get("Status", "In stock").fillna("In stock")
    lower_map = {
        "in maintenance": "In stock",
        "not used yet": "In stock",
        "disuse": "Out of use",
        "out of order": "Out of use",
        "in use": "In use",
    }
    df["Status"] = df["Status"].astype(str).str.strip().map(lambda s: lower_map.get(s.lower(), s))
    df["Status"] = df["Status"].apply(lambda s: s if s in STATUS_OPTIONS else "In stock")
    if "Year" in df.columns:
        try:
            df["Year"] = pd.to_numeric(df["Year"], errors="coerce").astype("Int64")
        except Exception:
            pass

    def compute_expiry(row):
        exp = row.get("Expiry", pd.NaT)
        try:
            parsed = pd.to_datetime(exp, errors="coerce")
            if pd.notna(parsed):
                return parsed
        except Exception:
            pass
        id_val = row.get("ID", "")
        id_date = parse_date_from_id(id_val)
        if pd.notna(id_date):
            return id_date + relativedelta(years=FIXED_EXPIRY_YEARS)
        return pd.NaT

    df["Expiry"] = df.apply(compute_expiry, axis=1)
    return df

def save_db(df):
    try:
        df.to_excel(EXCEL_PATH, index=False)
    except Exception:
        return False
    try:
        load_db.clear()
    except Exception:
        pass
    return True

# ---------------------------
# Filters
# ---------------------------
def apply_filters(df, user, context="default"):
    with st.expander("Filters", expanded=False):
        cols = ["Year", "Model", "Status"]
        if user.get("role") == "admin":
            cols += ["Client"]
        # For warnings: add WarningType
        if context == "warnings":
            if "WarningType" in df.columns:
                cols.append("WarningType")
        for col in cols:
            if col in df.columns:
                options = sorted([o for o in df[col].dropna().unique()])
                if options:
                    key = f"{context}_filter_{col}"
                    selected = st.multiselect(f"{col}", options, default=options, key=key)
                    if selected:
                        df = df[df[col].isin(selected)]
    return df

# ---------------------------
# Warnings & Expiration checks
# ---------------------------
def check_expirations(df, current_user):
    now = datetime.now()
    near_6m = now + relativedelta(months=6)
    near_1m = now + relativedelta(months=1)
    for _, row in df.iterrows():
        try:
            exp = pd.to_datetime(row.get("Expiry"), errors="coerce")
            if pd.isna(exp):
                if str(row.get("Status")).strip().lower() == "in use":
                    if current_user.get("role") == "admin" or row.get("Client") == current_user.get("client"):
                        subject = f"[Alert] Pump {row.get('Serial Number','?')} — Missing expiry"
                        body = f"Pump ID {row.get('Serial Number','?')} ({row.get('Model','?')}) has no expiry date assigned.\nPlease review."
                        send_email(current_user.get("email", current_user.get("username", "admin@localhost")), subject, body)
                continue
            if str(row.get("Status")).strip() not in ["In use", "In stock"]:
                continue
            subject = None
            if exp.date() <= now.date():
                subject = "Pump expired and still in use/stock"
            elif now < exp <= near_1m:
                subject = "Pump expiring within 1 month"
            elif now < exp <= near_6m:
                subject = "Pump expiring within 6 months"
            if subject and (current_user.get("role") == "admin" or row.get("Client") == current_user.get("client")):
                body = (f"Pump ID {row.get('Serial Number','?')} ({row.get('Model','?')}) flagged:\n\n"
                        f"Status: {row.get('Status')}\nExpiry: {exp.date()}\nClient: {row.get('Client','-')}\nNotes: {row.get('Notes','-')}")
                send_email(current_user.get("email", current_user.get("username", "admin@localhost")),
                           f"[Alert] {row.get('ID','?')} - {subject}", body)
        except Exception:
            continue
    # Additional CRONO SC patient checks
    cronos = df[df["Model"].str.contains("CRONO SC", case=False, na=False)]
    for _, r in cronos.iterrows():
        pid = r.get("Patient")
        if is_empty(pid):
            if current_user.get("role") == "admin" or r.get("Client") == current_user.get("client"):
                send_email(current_user.get("email", current_user.get("username", "admin@localhost")),
                           f"[Alert] CRONO SC {r.get('Serial Number','?')} — no patient assigned",
                           f"CRONO SC pump {r.get('Serial Number','?')} (serial {r.get('Serial Number','-')}) has no Patient assigned. Please assign a Patient (2 pumps per patient).")
    if not cronos.empty:
        patient_counts = cronos.groupby("Patient").size()
        for patient, count in patient_counts.items():
            if is_empty(patient):
                continue
            if count == 1:
                sample = cronos[cronos["Patient"] == patient].iloc[0]
                if current_user.get("role") == "admin" or sample.get("Client") == current_user.get("client"):
                    body = (f"Patient {patient} currently has only {count} CRONO SC pump assigned.\n\n"
                            f"Pump details:\n"
                            f"ID: {sample.get('ID','-')}\n"
                            f"Model: {sample.get('Model','-')}\n"
                            f"Client: {sample.get('Client','-')}\n"
                            f"Expiry: {sample.get('Expiry','-')}\n"
                            f"Status: {sample.get('Status','-')}\n"
                            f"Serial Number: {sample.get('Serial Number','-')}\n"
                            f"Notes: {sample.get('Notes','-')}")
                    send_email(current_user.get("email", current_user.get("username", "admin@localhost")),
                               f"[Alert] Patient {patient} has only 1 CRONO SC pump", body)

# ---------------------------
# Editable Pump UI
# ---------------------------
def current_user_email():
    return st.session_state.get("email", st.session_state.get("username", "admin@localhost"))

def render_editable_pump(row, idx, df):
    def safe_str(val):
        if pd.isna(val):
            return ""
        return str(val)
    def safe_int(val, default=0):
        try:
            if pd.isna(val) or val == "":
                return default
            return int(val)
        except Exception:
            return default
    def safe_date(val, fallback=None):
        try:
            parsed = pd.to_datetime(val, errors="coerce")
            if pd.notna(parsed):
                return parsed.date()
        except Exception:
            pass
        return fallback or datetime.now().date()

    expiry_default = safe_date(row.get("Expiry"))
    with st.expander(f"{safe_str(row.get('Serial Number'))} — {safe_str(row.get('Model'))}"):
        m = st.text_input("Model", value=safe_str(row.get("Model")), key=f"model_{idx}")
        y = st.text_input("Year", value=safe_str(row.get("Year")), key=f"year_{idx}")
        try:
            s_index = STATUS_OPTIONS.index(safe_str(row.get("Status"))) if safe_str(row.get("Status")) in STATUS_OPTIONS else 0
        except Exception:
            s_index = 0
        s = st.selectbox("Status", STATUS_OPTIONS, index=s_index, key=f"status_{idx}")
        c = st.text_input("Client", value=safe_str(row.get("Client")), key=f"client_{idx}")
        n = st.text_area("Notes", value=safe_str(row.get("Notes")), key=f"notes_{idx}")
        e = st.date_input("Expiry", value=expiry_default, key=f"expiry_{idx}")
        q_sold = st.number_input("Quantity Sold", value=safe_int(row.get("Quantity Sold")), min_value=0, step=1, key=f"qty_{idx}")
        serial = st.text_input("Serial Number", value=safe_str(row.get("Serial Number")), key=f"serial_{idx}")
        is_crono = "CRONO SC" in safe_str(m).upper()
        original_patient = row.get("Patient", pd.NA)
        original_patient_str = safe_str(original_patient)
        if is_crono:
            if is_empty(original_patient):
                patient_input = st.text_input("Patient (assign patient number) — REQUIRED", value="", key=f"patient_{idx}")
            else:
                patient_input = st.text_input("Patient (locked)", value=original_patient_str, key=f"patient_{idx}_locked", disabled=True)
        else:
            patient_input = st.text_input("Patient (only for CRONO SC)", value=original_patient_str, key=f"patient_{idx}", disabled=True)
        if st.button("Save Changes", key=f"save_{idx}"):
            if is_crono:
                assigned_patient = original_patient_str if (not is_empty(original_patient)) else st.session_state.get(f"patient_{idx}", "").strip()
                if is_empty(original_patient) and is_empty(assigned_patient):
                    st.error("CRONO SC pumps must be assigned to a patient. Please provide a patient number.")
                    return
                if (not is_empty(original_patient)) and (assigned_patient != original_patient_str):
                    st.error("Patient cannot be changed once assigned. Change prevented.")
                    send_email(current_user_email(),
                               f"[Warning] Attempted patient change on {safe_str(row.get('Serial Number'))}",
                               f"An attempt was made to change the patient on pump {safe_str(row.get('Serial Number'))}. Change was prevented.")
                    return
                existing_count = df[(df["Model"].str.contains("CRONO SC", case=False, na=False)) & (df["Patient"] == assigned_patient)].shape[0]
                if existing_count >= 2 and assigned_patient != original_patient_str:
                    st.error(f"Patient {assigned_patient} already has {existing_count} CRONO SC pumps. Max 2 allowed.")
                    return
            try:
                update_vals = {
                    "Model": m,
                    "Year": safe_int(y, default=row.get("Year", "")),
                    "Status": s,
                    "Client": c,
                    "Notes": n,
                    "Expiry": pd.Timestamp(e),
                    "Quantity Sold": safe_int(q_sold),
                    "Serial Number": serial,
                    "Last Updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
                for col, val in update_vals.items():
                    df.loc[df["ID"] == row["ID"], col] = val
                if is_crono:
                    new_patient_val = original_patient_str if (not is_empty(original_patient)) else st.session_state.get(f"patient_{idx}", "").strip()
                    df.loc[df["ID"] == row["ID"], "Patient"] = new_patient_val
                if save_db(df):
                    st.success("Saved!")
                    st.rerun()
                else:
                    st.error("Save failed.")
            except Exception as e:
                st.error(f"Error saving: {e}")

# ---------------------------
# Warnings panel (scrollable)
# ---------------------------
def render_warnings(df, user):
    st.subheader("Warnings")
    dfw = apply_filters(df.copy(), user, context="warnings")
    now = datetime.now()
    near_6m = now + relativedelta(months=6)
    expiry_parsed = pd.to_datetime(dfw["Expiry"], errors="coerce")
    cond_expiry_soon = (expiry_parsed <= near_6m) & (dfw["Status"].isin(["In use", "In stock"]))
    cond_missing_patient = (
        dfw["Model"].str.contains("CRONO SC", case=False, na=False) &
        (dfw["Patient"].fillna("").astype(str).str.strip() == "")
    )
    warning_df = dfw[cond_expiry_soon | cond_missing_patient].copy()
    warning_df["WarningType"] = None
    warning_df.loc[cond_expiry_soon, "WarningType"] = "Expiry"
    warning_df.loc[cond_missing_patient, "WarningType"] = "Missing patient"
    # Single-pump patients
    cronos = dfw[dfw["Model"].str.contains("CRONO SC", case=False, na=False)]
    if not cronos.empty:
        patient_counts = cronos.groupby("Patient").size()
        single_patients = [p for p, c in patient_counts.items() if (c == 1 and not is_empty(p))]
        for patient in single_patients:
            sample = cronos[cronos["Patient"] == patient].iloc[0].copy()
            sample["WarningType"] = f"Patient {patient} has only 1 CRONO SC pump"
            warning_df = pd.concat([warning_df, pd.DataFrame([sample])], ignore_index=True)
    if warning_df.empty:
        st.success("No warnings.")
        return

    # Filter by warning type
    warning_types = sorted(warning_df["WarningType"].dropna().unique())
    selected_types = st.multiselect("Filter by warning type", warning_types, default=warning_types)
    warning_df = warning_df[warning_df["WarningType"].isin(selected_types)]

    with st.container():
        st.markdown("<div style='max-height: 400px; overflow-y: auto;'>", unsafe_allow_html=True)
        for _, row in warning_df.iterrows():
            label = row.get("WarningType", "Warning")
            with st.expander(f"{label} — {row.get('Serial Number','?')}"):
                st.write("Model:", row.get("Model", "-"))
                st.write("Client:", row.get("Client", "-"))
                st.write("Expiry:", row.get("Expiry", "-"))
                st.write("Status:", row.get("Status", "-"))
                st.write("Patient:", row.get("Patient", "-"))
                st.write("Notes:", row.get("Notes", "-"))
        st.markdown("</div>", unsafe_allow_html=True)

# ---------------------------
# Load config and authenticator
# ---------------------------
with open("config.yaml") as f:
    config = yaml.load(f, Loader=SafeLoader)

authenticator = stauth.Authenticate(
    config["credentials"],
    config["cookie"]["name"],
    config["cookie"]["key"],
    config["cookie"]["expiry_days"]
)
authenticator.login(location="main", key="LoginForm")
name = st.session_state.get("name")
auth_status = st.session_state.get("authentication_status")
username = st.session_state.get("username")

# ---------------------------
# Main logic
# ---------------------------
if auth_status:
    authenticator.logout("Logout", "sidebar")
    st.sidebar.success(f"Welcome, {name}!")
    user = config["credentials"]["usernames"].get(username, {})
    role = user.get("role", "user")
    client = user.get("client")

    df = load_db()
    try:
        check_expirations(df, user)
    except Exception as e:
        st.error(f"Error while checking expirations: {e}")

    render_warnings(df, user)

    if role == "admin":
        tab1, tab2 = st.tabs(["Pump Registry", "Analytics"])
        with tab1:
            st.header("Pump Registry")
            df_filtered = apply_filters(df, user, context="admin_edit")
            with st.container():
                st.markdown("<div style='max-height: 550px; overflow-y: auto;'>", unsafe_allow_html=True)
                for idx, row in df_filtered.iterrows():
                    render_editable_pump(row, idx, df)
                st.markdown("</div>", unsafe_allow_html=True)

            st.markdown("---")
            st.subheader("Admin actions")
            if st.button("Recompute expiries from ID dates"):
                df["Expiry"] = df["ID"].apply(lambda i: (parse_date_from_id(i) + relativedelta(years=FIXED_EXPIRY_YEARS)) if pd.notna(parse_date_from_id(i)) else pd.NaT)
                if save_db(df):
                    st.success("Expiries updated.")
                    st.rerun()
                else:
                    st.error("Could not update expiries.")

        with tab2:
            st.header("Analytics")
            if "Year" in df.columns and "Quantity Sold" in df.columns:
                qty_by_year = df.groupby("Year", dropna=True)["Quantity Sold"].sum().reset_index()
            else:
                qty_by_year = pd.DataFrame()
            if not qty_by_year.empty:
                st.plotly_chart(
                    px.bar(qty_by_year.sort_values("Year"), x="Year", y="Quantity Sold",
                           title="Quantity Sold per Year", color_discrete_sequence=["#049484"]),
                    use_container_width=True
                )
            else:
                st.info("No sales data to show.")

            if {"Year", "Model", "Quantity Sold"}.issubset(df.columns):
                model_year = df.groupby(["Year", "Model"], dropna=True)["Quantity Sold"].sum().reset_index()
            else:
                model_year = pd.DataFrame()
            if not model_year.empty:
                fig = px.bar(model_year, x="Year", y="Quantity Sold", color="Model",
                             title="Quantity Sold by Model per Year")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No model/year data to show.")

            st.markdown("**Quantity Sold per Client**")
            if {"Client", "Quantity Sold"}.issubset(df.columns):
                client_sales = df.groupby("Client", dropna=True)["Quantity Sold"].sum().reset_index().sort_values("Quantity Sold", ascending=False)
            else:
                client_sales = pd.DataFrame()
            if not client_sales.empty:
                st.plotly_chart(
                    px.bar(client_sales, x="Client", y="Quantity Sold", title="Quantity Sold per Client",
                           color_discrete_sequence=["#049484"]),
                    use_container_width=True
                )
            else:
                st.info("No client sales data to show.")

            st.markdown("---")
            st.subheader("Raw data preview")
            st.dataframe(df.head(200), use_container_width=True)

    else:
        # Non-admin client view
        st.header(f"Client: {client}")
        user_df = df[df["Client"] == client]
        df_filtered = apply_filters(user_df, user, context="user_edit")
        with st.container():
            st.markdown("<div style='max-height: 550px; overflow-y: auto;'>", unsafe_allow_html=True)
            for idx, row in df_filtered.iterrows():
                render_editable_pump(row, idx, df)
            st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("---")
        st.subheader("Add New Pump")
        with st.form("add_pump_form"):
            new_id = st.text_input("Pump ID", key="new_id")
            new_model = st.text_input("Model", key="new_model")
            current_year = datetime.now().year
            new_year = st.number_input("Year", min_value=2020, max_value=current_year, step=1, key="new_year")
            new_status = st.selectbox("Status", STATUS_OPTIONS, key="new_status")
            new_notes = st.text_area("Notes", key="new_notes")
            new_qty = st.number_input("Quantity Sold", min_value=0, step=1, key="new_qty")
            new_serial = st.text_input("Serial Number", key="new_serial")
            default_id_date = parse_date_from_id(new_id) if not is_empty(new_id) else pd.NaT
            if pd.notna(default_id_date):
                default_expiry = (default_id_date + relativedelta(years=FIXED_EXPIRY_YEARS)).date()
            else:
                default_expiry = (datetime.now().date() + relativedelta(years=FIXED_EXPIRY_YEARS))
            new_expiry = st.date_input("Expiry Date", value=default_expiry, key="new_expiry")
            new_patient = "" if "CRONO SC" not in str(new_model).upper() else st.text_input("Patient (required for CRONO SC)", key="new_patient")
            submitted = st.form_submit_button("Add Pump")
            if submitted:
                if new_id.strip() == "" or new_model.strip() == "":
                    st.error("Pump ID and Model are required.")
                else:
                    if "CRONO SC" in str(new_model).upper():
                        if is_empty(new_patient):
                            st.error("CRONO SC pumps must have a patient assigned.")
                        else:
                            existing_count = df[(df["Model"].str.contains("CRONO SC", case=False, na=False)) & (df["Patient"] == new_patient)].shape[0]
                            if existing_count >= 2:
                                st.error(f"Patient {new_patient} already has {existing_count} CRONO SC pumps. Max 2 allowed.")
                            else:
                                new_row = {
                                    "ID": new_id,
                                    "Model": new_model,
                                    "Year": int(new_year),
                                    "Status": new_status,
                                    "Client": client,
                                    "Notes": new_notes,
                                    "Quantity Sold": int(new_qty),
                                    "Serial Number": new_serial,
                                    "Expiry": pd.Timestamp(new_expiry),
                                    "Patient": new_patient,
                                    "Last Updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                }
                                try:
                                    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                                    if save_db(df):
                                        st.success("Pump added!")
                                        st.rerun()
                                    else:
                                        st.error("Could not save new pump.")
                                except Exception as e:
                                    st.error(f"Error adding pump: {e}")
                    else:
                        new_row = {
                            "ID": new_id,
                            "Model": new_model,
                            "Year": int(new_year),
                            "Status": new_status,
                            "Client": client,
                            "Notes": new_notes,
                            "Quantity Sold": int(new_qty),
                            "Serial Number": new_serial,
                            "Expiry": pd.Timestamp(new_expiry),
                            "Patient": pd.NA,
                            "Last Updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        try:
                            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                            if save_db(df):
                                st.success("Pump added!")
                                st.rerun()
                            else:
                                st.error("Could not save new pump.")
                        except Exception as e:
                            st.error(f"Error adding pump: {e}")

else:
    st.error("Please log in or provide valid credentials.")
