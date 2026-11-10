import os
import pandas as pd
from store import DocumentStore

def export_processed_documents_to_excel(filename: str = "processed_documents.xlsx") -> str:
    store = DocumentStore()
    rows = store.fetch_all()
    
    data = []
    for row in rows:
        doc_id, source_file, json_data_str, status, created_at = row
        data.append({
            "ID": doc_id,
            "Файл-источник": source_file,
            "Статус": status,
            "Дата создания": created_at,
            "Данные JSON": json_data_str
        })
        
    df = pd.DataFrame(data)
    if df.empty:
        df = pd.DataFrame(columns=["ID", "Файл-источник", "Статус", "Дата создания", "Данные JSON"])
        
    df.to_excel(filename, index=False)
    return filename
