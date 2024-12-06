from main import app, db
from flask import Flask
import sqlite3
import os

def migrate_database():
    with app.app_context():
        try:
            # Cek apakah file database ada
            if os.path.exists('adms.db'):
                # Backup database lama
                if os.path.exists('adms.db.backup'):
                    os.remove('adms.db.backup')
                os.rename('adms.db', 'adms.db.backup')
                print("Database lama di-backup ke adms.db.backup")
            
            # Buat database baru dengan skema yang diperbarui
            db.create_all()
            print("Database baru berhasil dibuat dengan skema terbaru")
            
            # Jika ada backup, coba impor data lama
            if os.path.exists('adms.db.backup'):
                try:
                    # Koneksi ke database backup
                    backup_conn = sqlite3.connect('adms.db.backup')
                    backup_cursor = backup_conn.cursor()
                    
                    # Cek apakah tabel i_clock_machine ada di backup
                    backup_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='i_clock_machine'")
                    if backup_cursor.fetchone():
                        # Ambil data dari backup
                        backup_cursor.execute("SELECT id, serial_number, last_seen, timezone FROM i_clock_machine")
                        existing_machines = backup_cursor.fetchall()
                        
                        # Koneksi ke database baru
                        new_conn = sqlite3.connect('adms.db')
                        new_cursor = new_conn.cursor()
                        
                        # Masukkan data ke database baru
                        for machine in existing_machines:
                            new_cursor.execute(
                                "INSERT INTO i_clock_machine (id, serial_number, name, last_seen, timezone) VALUES (?, ?, ?, ?, ?)",
                                (machine[0], machine[1], f"Mesin {machine[1]}", machine[2], machine[3])
                            )
                        
                        new_conn.commit()
                        new_conn.close()
                        print(f"Berhasil memindahkan {len(existing_machines)} data mesin dari backup")
                    
                    backup_conn.close()
                    print("Migrasi data dari backup selesai")
                    
                except Exception as e:
                    print(f"Error saat mengimpor data dari backup: {str(e)}")
                    print("Melanjutkan dengan database baru tanpa data lama")
            
            print("Migrasi database berhasil!")
            
        except Exception as e:
            print(f"Error saat migrasi: {str(e)}")
            # Kembalikan backup jika ada error
            if os.path.exists('adms.db.backup') and os.path.exists('adms.db'):
                os.remove('adms.db')
                os.rename('adms.db.backup', 'adms.db')
                print("Database dikembalikan dari backup")

if __name__ == "__main__":
    migrate_database() 
