package basestore

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/franchyi/sandboxfs/internal/control"
)

type Store struct {
	mu      sync.RWMutex
	path    string
	records map[string]control.BaseRecord
}

func Open(path string) (*Store, error) {
	store := &Store{path: path, records: make(map[string]control.BaseRecord)}
	payload, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return store, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read base registry: %w", err)
	}
	if len(payload) == 0 {
		return store, nil
	}
	if err := json.Unmarshal(payload, &store.records); err != nil {
		return nil, fmt.Errorf("decode base registry: %w", err)
	}
	return store, nil
}

func (s *Store) Register(name, path string) (control.BaseRecord, error) {
	if !validName(name) {
		return control.BaseRecord{}, fmt.Errorf("invalid base name %q", name)
	}
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		return control.BaseRecord{}, fmt.Errorf("resolve base path: %w", err)
	}
	resolved, err = filepath.Abs(resolved)
	if err != nil {
		return control.BaseRecord{}, fmt.Errorf("make base path absolute: %w", err)
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return control.BaseRecord{}, fmt.Errorf("stat base: %w", err)
	}
	if !info.IsDir() {
		return control.BaseRecord{}, fmt.Errorf("base is not a directory: %s", resolved)
	}

	digest, files, logicalBytes, err := TreeDigest(resolved)
	if err != nil {
		return control.BaseRecord{}, err
	}
	record := control.BaseRecord{
		Name:         name,
		Path:         resolved,
		Digest:       digest,
		Files:        files,
		LogicalBytes: logicalBytes,
		RegisteredAt: control.Timestamp(time.Now()),
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if existing, found := s.records[name]; found && existing.References > 0 {
		return control.BaseRecord{}, fmt.Errorf("base %q has %d active references", name, existing.References)
	}
	s.records[name] = record
	if err := s.saveLocked(); err != nil {
		return control.BaseRecord{}, err
	}
	return record, nil
}

func (s *Store) Get(name string) (control.BaseRecord, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	record, found := s.records[name]
	return record, found
}

func (s *Store) List() []control.BaseRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	result := make([]control.BaseRecord, 0, len(s.records))
	for _, record := range s.records {
		result = append(result, record)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Name < result[j].Name })
	return result
}

func (s *Store) AddReference(name string, delta int) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	record, found := s.records[name]
	if !found {
		return fmt.Errorf("unknown base %q", name)
	}
	record.References += delta
	if record.References < 0 {
		record.References = 0
	}
	s.records[name] = record
	return s.saveLocked()
}

func (s *Store) ResetReferences() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for name, record := range s.records {
		record.References = 0
		s.records[name] = record
	}
	return s.saveLocked()
}

func (s *Store) Verify(name string) (control.VerifyBaseResponse, error) {
	record, found := s.Get(name)
	if !found {
		return control.VerifyBaseResponse{}, fmt.Errorf("unknown base %q", name)
	}
	digest, _, _, err := TreeDigest(record.Path)
	if err != nil {
		return control.VerifyBaseResponse{}, err
	}
	return control.VerifyBaseResponse{
		Name:     name,
		Expected: record.Digest,
		Actual:   digest,
		Match:    digest == record.Digest,
	}, nil
}

func (s *Store) saveLocked() error {
	if err := os.MkdirAll(filepath.Dir(s.path), 0o750); err != nil {
		return fmt.Errorf("create registry directory: %w", err)
	}
	temp, err := os.CreateTemp(filepath.Dir(s.path), ".bases-*.json")
	if err != nil {
		return fmt.Errorf("create registry temp file: %w", err)
	}
	tempName := temp.Name()
	defer os.Remove(tempName)
	encoder := json.NewEncoder(temp)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(s.records); err != nil {
		temp.Close()
		return fmt.Errorf("encode registry: %w", err)
	}
	if err := temp.Sync(); err != nil {
		temp.Close()
		return fmt.Errorf("sync registry: %w", err)
	}
	if err := temp.Close(); err != nil {
		return fmt.Errorf("close registry: %w", err)
	}
	if err := os.Chmod(tempName, 0o640); err != nil {
		return fmt.Errorf("chmod registry: %w", err)
	}
	if err := os.Rename(tempName, s.path); err != nil {
		return fmt.Errorf("replace registry: %w", err)
	}
	return nil
}

func TreeDigest(root string) (digest string, files, logicalBytes int64, returnErr error) {
	hash := sha256.New()
	writer := bufio.NewWriter(hash)
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if relative == "." {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if _, err := fmt.Fprintf(writer, "%s\x00%s\x00%o\x00%d\x00", relative, entry.Type().String(), info.Mode().Perm(), info.Size()); err != nil {
			return err
		}
		switch {
		case entry.Type()&os.ModeSymlink != 0:
			target, err := os.Readlink(path)
			if err != nil {
				return err
			}
			if _, err := writer.WriteString(target); err != nil {
				return err
			}
		case entry.Type().IsRegular():
			file, err := os.Open(path)
			if err != nil {
				return err
			}
			_, copyErr := io.Copy(writer, file)
			closeErr := file.Close()
			if copyErr != nil {
				return copyErr
			}
			if closeErr != nil {
				return closeErr
			}
			files++
			logicalBytes += info.Size()
		}
		_, err = writer.WriteString("\x00")
		return err
	})
	if err != nil {
		return "", 0, 0, fmt.Errorf("digest base tree: %w", err)
	}
	if err := writer.Flush(); err != nil {
		return "", 0, 0, fmt.Errorf("flush base digest: %w", err)
	}
	return hex.EncodeToString(hash.Sum(nil)), files, logicalBytes, nil
}

func validName(name string) bool {
	if len(name) == 0 || len(name) > 64 {
		return false
	}
	for index, character := range name {
		if (character >= 'a' && character <= 'z') ||
			(character >= 'A' && character <= 'Z') ||
			(character >= '0' && character <= '9') ||
			(index > 0 && strings.ContainsRune("._-", character)) {
			continue
		}
		return false
	}
	return true
}
