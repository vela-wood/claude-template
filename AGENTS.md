I am an expert attorney that uses command-line tools (e.g. bash, osgrep) to find relevant files and translate them into a format I understand.
Use `uv run` to execute all python commands and scripts, e.g., uv run markitdown
Use the below tools to read the corresponding file extensions:
- markitdown: .pdf, .docx, .doc, .pptx, .ppt, .zip
- pandas: .csv, .xls, .xlsx
For any other file extensions which are not plain text, use your best judgment. 
Save the output of any markitdown converstion to a filename that has ".md" appended at the end to save time on conversions in the future. If a .md version of a file already exists, always use that instead of converting again.

Below is the relevant output of `uv run markitdown -h`
usage: SYNTAX:

    markitdown <OPTIONAL: FILENAME>
    If FILENAME is empty, markitdown reads from stdin.

EXAMPLE:

    markitdown example.pdf

    OR

    cat example.pdf | markitdown

    OR

    markitdown < example.pdf

    OR to save to a file use

    markitdown example.pdf -o example.md

    OR

    markitdown example.pdf > example.md

Convert various file formats to markdown.