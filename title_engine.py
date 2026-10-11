import random

def analyze_property_documents(uploaded_files):
    """
    Simulates AI extraction from uploaded deeds and EC documents.
    Returns structured data for the Streamlit dashboard.
    """
    # Simulated extracted data based on documents
    extracted_data = {
        "health_score": 82,
        "risk_status": "Low-Medium Risk",
        "identifiers": {
            "state": "Maharashtra",
            "district": "Pune / Haveli",
            "survey_no": "142/2B, Plot 12",
            "khata_id": "MH-PN-2024-9821"
        },
        "red_flags": [
            "⚠️ Gap in EC Records: Transaction entry missing between 2008 and 2012 in Encumbrance Certificate."
        ],
        "positive_flags": [
            "✅ Current Owner Match: Seller name on Sale Deed matches 7/12 extract perfectly.",
            "✅ Mortgage Status: No active bank liens or court attachments found in 30-year EC."
        ],
        "timeline": [
            {"Year": "1994", "Event": "Original Allotment / Sale", "Details": "Acquired by Mr. Ramesh Sharma from MIDC", "Status": "Verified ✅"},
            {"Year": "2008", "Event": "Sale Deed Execution", "Details": "Sold by Ramesh Sharma to Mrs. Sunita Patil", "Status": "Verified ✅"},
            {"Year": "2012", "Event": "Mutation / Transfer", "Details": "⚠️ Gap or unregistered family transfer observed before 2015", "Status": "Review Required ⚠️"},
            {"Year": "2018", "Event": "Gift Deed / Sale", "Details": "Transferred to Mr. Vikram Patil", "Status": "Verified ✅"},
            {"Year": "2024", "Event": "Current Proposed Sale", "Details": "Vikram Patil selling to Prospective Buyer (You)", "Status": "Active Transaction"}
        ],
        "checklist": [
            {"Document": "Title Deed (Sale Deed 2018)", "Status": "Verified", "Confidence": "98%", "Remarks": "Signatures, stamp duty, and boundaries intact."},
            {"Document": "Encumbrance Certificate (30 Yrs)", "Status": "Review Needed", "Confidence": "89%", "Remarks": "Missing entry verification required for 2008-2012 period."},
            {"Document": "Land Record (7/12 Extract)", "Status": "Verified", "Confidence": "95%", "Remarks": "Area matches deed (2,400 sq. ft.). No tenant disputes."},
            {"Document": "Property Tax Receipts", "Status": "Up to Date", "Confidence": "100%", "Remarks": "Paid up to Financial Year 2025-26."},
            {"Document": "Building Plan Sanction / OC", "Status": "Pending Upload", "Confidence": "--", "Remarks": "Not provided in current batch."}
        ]
    }
    return extracted_data