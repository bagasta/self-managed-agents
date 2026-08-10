import os
from pathlib import Path

def resolve_and_replace_symlink_files(root_dir: str):
    root = Path(root_dir).resolve()
    fixed_count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        
        # Check if the file content is just a relative path pointing to another file
        if (content.startswith("../") or content.startswith("./")) and "\n" not in content and len(content) < 300:
            target_path = (path.parent / content).resolve()
            if target_path.exists() and target_path.is_file():
                print(f"Replacing symlink stub {path.relative_to(root)} -> {target_path.relative_to(root)}")
                target_bytes = target_path.read_bytes()
                path.write_bytes(target_bytes)
                fixed_count += 1

    print(f"\nSuccessfully fixed {fixed_count} symlink stub files.")

if __name__ == "__main__":
    project_root = Path(__file__).parent.parent
    resolve_and_replace_symlink_files(project_root)
