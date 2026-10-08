package basestore

import (
	"os"
	"path/filepath"
	"testing"
)

func TestRegisterAndVerify(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "base")
	if err := os.MkdirAll(filepath.Join(base, "nested"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(base, "nested", "file.txt"), []byte("one"), 0o644); err != nil {
		t.Fatal(err)
	}
	store, err := Open(filepath.Join(root, "registry", "bases.json"))
	if err != nil {
		t.Fatal(err)
	}
	record, err := store.Register("base-one", base)
	if err != nil {
		t.Fatal(err)
	}
	if record.Files != 1 || record.LogicalBytes != 3 || record.Digest == "" {
		t.Fatalf("unexpected record: %+v", record)
	}
	verification, err := store.Verify("base-one")
	if err != nil || !verification.Match {
		t.Fatalf("unexpected verification: %+v, %v", verification, err)
	}
	if err := os.WriteFile(filepath.Join(base, "nested", "file.txt"), []byte("two"), 0o644); err != nil {
		t.Fatal(err)
	}
	verification, err = store.Verify("base-one")
	if err != nil || verification.Match {
		t.Fatalf("expected digest mismatch: %+v, %v", verification, err)
	}
}
