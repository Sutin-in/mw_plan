from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter(prefix="/api/admin", tags=["Admin Permission Management"])

class UserRoleUpdate(BaseModel):
    role_id: int

class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    role_id: Optional[int] = None

    class Config:
        orm_mode = True

# ตัวอย่าง Dependency ตรวจสอบสิทธิ์ Admin
def verify_admin_user(current_user = Depends(...)):
    if getattr(current_user, "role_name", None) != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="สิทธิ์การใช้งานไม่เพียงพอ เฉพาะผู้ดูแลระบบ (Admin) เท่านั้น"
        )
    return current_user

@router.get("/users", response_model=List[UserResponse])
def get_all_users_with_roles(db: Session = Depends(...), admin = Depends(verify_admin_user)):
    # ดึงรายชื่อผู้ใช้ทั้งหมดจากระบบ
    users = db.execute("SELECT id, username, email, role_id FROM users").fetchall()
    return [UserResponse(id=u[0], username=u[1], email=u[2], role_id=u[3]) for u in users]

@router.put("/users/{user_id}/role", response_model=UserResponse)
def update_user_role(user_id: int, payload: UserRoleUpdate, db: Session = Depends(...), admin = Depends(verify_admin_user)):
    # อัปเดต role_id ให้กับผู้ใช้งาน
    result = db.execute("SELECT id, username, email, role_id FROM users WHERE id = :id", {"id": user_id}).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="ไม่พบผู้ใช้งานนี้ในระบบ")
    
    db.execute("UPDATE users SET role_id = :role_id WHERE id = :id", {"role_id": payload.role_id, "id": user_id})
    db.commit()
    
    updated_user = db.execute("SELECT id, username, email, role_id FROM users WHERE id = :id", {"id": user_id}).fetchone()
    return UserResponse(id=updated_user[0], username=updated_user[1], email=updated_user[2], role_id=updated_user[3])