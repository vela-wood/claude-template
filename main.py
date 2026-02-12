import os

from dotenv import load_dotenv


def main():
    load_dotenv()
    print("Python environment configured.")
    print("Tools available to agent...")
    nd_vars = ("MATTERS_DB", "ND_API_KEY", "NDHELPER_URL")
    if all(os.getenv(var) for var in nd_vars):
        print("\tNetdocs access")
    
    artifact_vars = ("ARTIFACT_API_TOKEN", "ARTIFACT_URL")
    if all(os.getenv(var) for var in artifact_vars):
        print("\tPDF artifact removal")

if __name__ == "__main__":
    main()
