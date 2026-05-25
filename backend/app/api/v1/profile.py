from fastapi import APIRouter, Depends, HTTPException, status, File, UploadFile
from typing import Dict, Any, List, Optional
import logging
import uuid
import base64
from datetime import datetime
from PIL import Image
import io

from ...core.auth import authenticate_request
from ...models.auth import AuthenticatedUser
from ...models.profile import (
    UserProfile, UserProfileUpdate, UserPreferences, UserPreferencesUpdate,
    NotificationPreference, NotificationPreferenceUpdate, AvatarUploadResponse,
    ProfileResponse
)
from ...database import supabase

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profile", tags=["profile"])

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
AVATAR_SIZE = (300, 300)  # Max avatar dimensions

_LOCAL_PROFILE_STORE: Dict[str, Dict[str, Any]] = {}
_LOCAL_PREFERENCES_STORE: Dict[str, Dict[str, Any]] = {}
_LOCAL_NOTIFICATION_PREFERENCES_STORE: Dict[str, Dict[str, Dict[str, Any]]] = {}


def allowed_file(filename: str) -> bool:
    """Check if the file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def synthetic_id(prefix: str, user_id: str, extra: str = "") -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"propertyflow:{prefix}:{user_id}:{extra}"))


def default_profile_data(user: AuthenticatedUser) -> Dict[str, Any]:
    timestamp = now_iso()
    return {
        'id': synthetic_id('profile', user.id),
        'user_id': user.id,
        'display_name': (user.email.split('@')[0] if user.email else 'User'),
        'bio': None,
        'phone': None,
        'department': None,
        'job_title': None,
        'location': None,
        'timezone': 'UTC',
        'language': 'en',
        'theme': 'light',
        'avatar_url': None,
        'created_at': timestamp,
        'updated_at': timestamp,
    }


def default_preferences_data(user: AuthenticatedUser) -> Dict[str, Any]:
    timestamp = now_iso()
    return {
        'id': synthetic_id('preferences', user.id),
        'user_id': user.id,
        'notification_email': True,
        'notification_push': True,
        'notification_desktop': True,
        'notification_sound': True,
        'auto_refresh': True,
        'compact_view': False,
        'sidebar_collapsed': False,
        'created_at': timestamp,
        'updated_at': timestamp,
    }


def local_profile_row(user: AuthenticatedUser) -> Dict[str, Any]:
    if user.id not in _LOCAL_PROFILE_STORE:
        _LOCAL_PROFILE_STORE[user.id] = default_profile_data(user)
    return dict(_LOCAL_PROFILE_STORE[user.id])


def local_preferences_row(user: AuthenticatedUser) -> Dict[str, Any]:
    if user.id not in _LOCAL_PREFERENCES_STORE:
        _LOCAL_PREFERENCES_STORE[user.id] = default_preferences_data(user)
    return dict(_LOCAL_PREFERENCES_STORE[user.id])


def update_local_profile(user: AuthenticatedUser, update_data: Dict[str, Any]) -> Dict[str, Any]:
    row = local_profile_row(user)
    row.update(update_data)
    row['updated_at'] = now_iso()
    _LOCAL_PROFILE_STORE[user.id] = row
    return dict(row)


def update_local_preferences(user: AuthenticatedUser, update_data: Dict[str, Any]) -> Dict[str, Any]:
    row = local_preferences_row(user)
    row.update(update_data)
    row['updated_at'] = now_iso()
    _LOCAL_PREFERENCES_STORE[user.id] = row
    return dict(row)


def update_local_notification_preference(
    user: AuthenticatedUser,
    category: str,
    update_data: Dict[str, Any],
) -> Dict[str, Any]:
    user_preferences = _LOCAL_NOTIFICATION_PREFERENCES_STORE.setdefault(user.id, {})
    timestamp = now_iso()
    row = user_preferences.get(category, {
        'id': synthetic_id('notification-preference', user.id, category),
        'user_id': user.id,
        'category': category,
        'email_enabled': True,
        'push_enabled': True,
        'desktop_enabled': True,
        'sound_enabled': True,
        'created_at': timestamp,
        'updated_at': timestamp,
    })
    row.update(update_data)
    row['updated_at'] = timestamp
    user_preferences[category] = row
    return dict(row)


def local_notification_preferences(user: AuthenticatedUser) -> List[Dict[str, Any]]:
    return [
        dict(preference)
        for preference in _LOCAL_NOTIFICATION_PREFERENCES_STORE.get(user.id, {}).values()
    ]


def response_data(response: Any) -> List[Dict[str, Any]]:
    data = getattr(response, 'data', None)
    return data if isinstance(data, list) else []


def with_profile_defaults(user: AuthenticatedUser, data: Dict[str, Any]) -> Dict[str, Any]:
    row = default_profile_data(user)
    row.update({key: value for key, value in data.items() if value is not None})
    return row


def with_preferences_defaults(user: AuthenticatedUser, data: Dict[str, Any]) -> Dict[str, Any]:
    row = default_preferences_data(user)
    row.update({key: value for key, value in data.items() if value is not None})
    return row


async def upsert_profile_row(user: AuthenticatedUser, update_data: Dict[str, Any]) -> Dict[str, Any]:
    update_payload = {**update_data, 'updated_at': now_iso()}

    try:
        response = supabase.table('user_profiles').update(update_payload).eq('user_id', user.id).execute()
        data = response_data(response)
        if data:
            return with_profile_defaults(user, data[0])

        create_data = with_profile_defaults(user, update_payload)
        response = supabase.table('user_profiles').insert(create_data).execute()
        data = response_data(response)
        if data:
            return with_profile_defaults(user, data[0])

        logger.info(f"Profile row unavailable for user {user.id}; using local fallback store")
    except Exception as db_error:
        logger.warning(f"Profile upsert fell back to local store for user {user.id}: {db_error}")

    return update_local_profile(user, update_payload)


async def upsert_preferences_row(user: AuthenticatedUser, update_data: Dict[str, Any]) -> Dict[str, Any]:
    update_payload = {**update_data, 'updated_at': now_iso()}

    try:
        response = supabase.table('user_preferences').update(update_payload).eq('user_id', user.id).execute()
        data = response_data(response)
        if data:
            return with_preferences_defaults(user, data[0])

        create_data = with_preferences_defaults(user, update_payload)
        response = supabase.table('user_preferences').insert(create_data).execute()
        data = response_data(response)
        if data:
            return with_preferences_defaults(user, data[0])

        logger.info(f"Preferences row unavailable for user {user.id}; using local fallback store")
    except Exception as db_error:
        logger.warning(f"Preferences upsert fell back to local store for user {user.id}: {db_error}")

    return update_local_preferences(user, update_payload)


async def upsert_notification_preference_row(
    user: AuthenticatedUser,
    category: str,
    update_data: Dict[str, Any],
) -> Dict[str, Any]:
    update_payload = {**update_data, 'updated_at': now_iso()}

    try:
        response = (
            supabase.table('notification_preferences')
            .update(update_payload)
            .eq('user_id', user.id)
            .eq('category', category)
            .execute()
        )
        data = response_data(response)
        if data:
            return data[0]

        create_data = {
            'id': synthetic_id('notification-preference', user.id, category),
            'user_id': user.id,
            'category': category,
            'email_enabled': True,
            'push_enabled': True,
            'desktop_enabled': True,
            'sound_enabled': True,
            'created_at': now_iso(),
            **update_payload,
        }
        response = supabase.table('notification_preferences').insert(create_data).execute()
        data = response_data(response)
        if data:
            return data[0]

        logger.info(f"Notification preference unavailable for user {user.id}; using local fallback store")
    except Exception as db_error:
        logger.warning(f"Notification preference upsert fell back to local store for user {user.id}: {db_error}")

    return update_local_notification_preference(user, category, update_payload)

def resize_image(image_data: bytes, size: tuple = AVATAR_SIZE) -> bytes:
    """Resize image to specified dimensions while maintaining aspect ratio"""
    try:
        image = Image.open(io.BytesIO(image_data))
        
        # Convert to RGB if necessary (for PNG with transparency)
        if image.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', image.size, (255, 255, 255))
            if image.mode == 'P':
                image = image.convert('RGBA')
            background.paste(image, mask=image.split()[-1] if image.mode == 'RGBA' else None)
            image = background
        
        # Resize while maintaining aspect ratio
        image.thumbnail(size, Image.Resampling.LANCZOS)
        
        # Save to bytes
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=85, optimize=True)
        return output.getvalue()
    
    except Exception as e:
        logger.error(f"Error resizing image: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image file"
        )

@router.get("", response_model=ProfileResponse)
async def get_profile(
    user: AuthenticatedUser = Depends(authenticate_request)
):
    """Get current user's profile, preferences, and notification settings"""
    try:
        logger.info(f"User {user.email} is fetching their profile.")
        
        profile = None
        preferences = None
        notification_preferences = []
        unread_count = 0
        
        # Try to get user profile
        try:
            profile_response = supabase.table('user_profiles').select('*').eq('user_id', user.id).execute()
            if profile_response.data:
                profile_data = profile_response.data[0]
                profile = UserProfile(**with_profile_defaults(user, profile_data))
            else:
                logger.info(f"No profile found for user {user.id}, using default profile")
                profile = UserProfile(**local_profile_row(user))
        except Exception as profile_error:
            logger.warning(f"Error accessing user_profiles table for user {user.id}: {profile_error}")
            logger.info(f"Using default profile for user {user.id}")
            profile = UserProfile(**local_profile_row(user))
        
        # Try to get user preferences
        try:
            preferences_response = supabase.table('user_preferences').select('*').eq('user_id', user.id).execute()
            if preferences_response.data:
                preferences_data = preferences_response.data[0]
                preferences = UserPreferences(**with_preferences_defaults(user, preferences_data))
            else:
                logger.info(f"No preferences found for user {user.id}, using default preferences")
                preferences = UserPreferences(**local_preferences_row(user))
        except Exception as preferences_error:
            logger.warning(f"Error accessing user_preferences table for user {user.id}: {preferences_error}")
            logger.info(f"Using default preferences for user {user.id}")
            preferences = UserPreferences(**local_preferences_row(user))
        
        # Try to get notification preferences
        try:
            notification_prefs_response = supabase.table('notification_preferences').select('*').eq('user_id', user.id).execute()
            notification_rows = notification_prefs_response.data or local_notification_preferences(user)
            notification_preferences = [NotificationPreference(**pref) for pref in notification_rows]
        except Exception as notif_error:
            logger.warning(f"Error accessing notification_preferences table for user {user.id}: {notif_error}")
            logger.info(f"Using empty notification preferences for user {user.id}")
            notification_preferences = [
                NotificationPreference(**pref)
                for pref in local_notification_preferences(user)
            ]
        
        # Try to get unread notification count
        try:
            unread_response = supabase.rpc('get_unread_notification_count', {'user_uuid': user.id}).execute()
            data = unread_response.data
            if isinstance(data, list):
                # Handle mock/list response
                unread_count = len(data) if data else 0
            else:
                unread_count = data if data is not None else 0
        except Exception as unread_error:
            logger.warning(f"Error getting unread notification count for user {user.id}: {unread_error}")
            unread_count = 0
        
        logger.info(f"Successfully fetched/created profile for user {user.id}")
        
        return ProfileResponse(
            profile=profile,
            preferences=preferences,
            notification_preferences=notification_preferences,
            unread_count=unread_count
        )
        
    except Exception as e:
        logger.error(f"Error fetching profile for user {user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching profile."
        )

@router.put("", response_model=UserProfile)
async def update_profile(
    profile_update: UserProfileUpdate,
    user: AuthenticatedUser = Depends(authenticate_request)
):
    """Update current user's profile information"""
    try:
        logger.info(f"User {user.email} is updating their profile.")
        
        # Prepare update data (only include non-None values)
        update_data = {}
        for field, value in profile_update.dict(exclude_unset=True).items():
            if value is not None:
                update_data[field] = value
        
        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid fields to update"
            )
        
        updated_profile = UserProfile(**await upsert_profile_row(user, update_data))
        logger.info(f"Successfully updated profile for user {user.id}")
        
        return updated_profile
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating profile for user {user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating profile."
        )

@router.put("/preferences", response_model=UserPreferences)
async def update_preferences(
    preferences_update: UserPreferencesUpdate,
    user: AuthenticatedUser = Depends(authenticate_request)
):
    """Update current user's UI and general preferences"""
    try:
        logger.info(f"User {user.email} is updating their preferences.")
        
        # Prepare update data
        update_data = preferences_update.dict(exclude_unset=True)
        
        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid fields to update"
            )
        
        updated_preferences = UserPreferences(**await upsert_preferences_row(user, update_data))
        logger.info(f"Successfully updated preferences for user {user.id}")
        
        return updated_preferences
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating preferences for user {user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating preferences."
        )

@router.put("/notification-preferences/{category}", response_model=NotificationPreference)
async def update_notification_preference(
    category: str,
    preference_update: NotificationPreferenceUpdate,
    user: AuthenticatedUser = Depends(authenticate_request)
):
    """Update notification preferences for a specific category"""
    try:
        logger.info(f"User {user.email} is updating notification preferences for category {category}.")
        
        # Prepare update data
        update_data = preference_update.dict(exclude_unset=True)
        
        if not update_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid fields to update"
            )
        
        updated_preference = NotificationPreference(
            **await upsert_notification_preference_row(user, category, update_data)
        )
        logger.info(f"Successfully updated notification preferences for user {user.id}, category {category}")
        
        return updated_preference
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating notification preferences for user {user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating notification preferences."
        )

@router.post("/avatar", response_model=AvatarUploadResponse)
async def upload_avatar(
    file: UploadFile = File(...),
    user: AuthenticatedUser = Depends(authenticate_request)
):
    """Upload and set user avatar image"""
    try:
        logger.info(f"User {user.email} is uploading an avatar.")
        
        # Validate file
        if not file.filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No file selected"
            )
        
        if not allowed_file(file.filename):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File type not allowed. Allowed types: {', '.join(ALLOWED_EXTENSIONS)}"
            )
        
        # Read and validate file size
        file_content = await file.read()
        if len(file_content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File too large. Maximum size: {MAX_FILE_SIZE // (1024*1024)}MB"
            )
        
        # Resize image
        resized_image = resize_image(file_content)
        
        unique_filename = f"{user.id}/avatar_{uuid.uuid4().hex}.jpg"  # Always save as JPEG after processing
        public_url: Optional[str] = None

        try:
            try:
                # Delete existing avatar if exists
                existing_files = supabase.storage.from_('profile-pictures').list(user.id)
                if existing_files:
                    for existing_file in existing_files:
                        if existing_file['name'].startswith('avatar_'):
                            supabase.storage.from_('profile-pictures').remove([f"{user.id}/{existing_file['name']}"])
                            logger.info(f"Deleted existing avatar: {existing_file['name']}")
            except Exception as delete_error:
                logger.warning(f"Could not delete existing avatar: {delete_error}")

            upload_response = supabase.storage.from_('profile-pictures').upload(
                unique_filename,
                resized_image,
                file_options={'content-type': 'image/jpeg'}
            )

            status_code = getattr(upload_response, 'status_code', 200)
            if status_code >= 400:
                raise RuntimeError(f"Storage upload failed with status {status_code}: {upload_response}")

            public_url = supabase.storage.from_('profile-pictures').get_public_url(unique_filename)
        except Exception as storage_error:
            logger.warning(f"Avatar storage unavailable for user {user.id}; using local data URL fallback: {storage_error}")
            encoded = base64.b64encode(resized_image).decode('ascii')
            public_url = f"data:image/jpeg;base64,{encoded}"

        await upsert_profile_row(user, {'avatar_url': public_url})

        logger.info(f"Successfully uploaded avatar for user {user.id}")

        return AvatarUploadResponse(
            avatar_url=public_url,
            message="Avatar uploaded successfully"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uploading avatar for user {user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while uploading avatar."
        )

@router.delete("/avatar")
async def delete_avatar(
    user: AuthenticatedUser = Depends(authenticate_request)
):
    """Delete user's current avatar"""
    try:
        logger.info(f"User {user.email} is deleting their avatar.")

        avatar_url = local_profile_row(user).get('avatar_url')
        try:
            profile_response = supabase.table('user_profiles').select('avatar_url').eq('user_id', user.id).execute()
            data = response_data(profile_response)
            if data:
                avatar_url = data[0].get('avatar_url')
        except Exception as profile_error:
            logger.warning(f"Could not fetch avatar from profile store for user {user.id}: {profile_error}")

        if avatar_url and not str(avatar_url).startswith('data:'):
            try:
                existing_files = supabase.storage.from_('profile-pictures').list(user.id)
                if existing_files:
                    files_to_delete = [f"{user.id}/{file['name']}" for file in existing_files if file['name'].startswith('avatar_')]
                    if files_to_delete:
                        supabase.storage.from_('profile-pictures').remove(files_to_delete)
                        logger.info(f"Deleted avatar files: {files_to_delete}")
            except Exception as delete_error:
                logger.warning(f"Could not delete avatar files: {delete_error}")

        await upsert_profile_row(user, {'avatar_url': None})

        logger.info(f"Successfully deleted avatar for user {user.id}")

        return {"message": "Avatar deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting avatar for user {user.id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting avatar."
        )
