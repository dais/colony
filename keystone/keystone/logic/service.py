# Copyright (c) 2010-2011 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from datetime import datetime, timedelta
import uuid
import logging

from keystone.logic.types import auth, atom
from keystone.logic.signer import Signer
import keystone.backends as backends
import keystone.backends.api as api
import keystone.backends.models as models
from keystone.logic.types import fault
from keystone.logic.types.tenant import Tenant, Tenants
from keystone.logic.types.role import Role, RoleRef, RoleRefs, Roles, \
    UserRole, UserRoles
from keystone.logic.types.service import Service, Services
from keystone.logic.types.user import User, User_Update, Users
from keystone.logic.types.endpoint import Endpoint, Endpoints, \
    EndpointTemplate, EndpointTemplates


LOG = logging.getLogger('keystone.logic.service')


class IdentityService(object):
    """Implements Identity service"""

    #
    #  Token Operations
    #
    def authenticate(self, auth_request):
        # Check auth_with_password_credentials
        if not isinstance(auth_request, auth.AuthWithPasswordCredentials):
            raise fault.BadRequestFault(
                "Expecting auth_with_password_credentials!")

        def validate(duser):
            return api.USER.check_password(duser, auth_request.password)

        if auth_request.tenant_name:
            dtenant = self.__validate_tenant_by_name(auth_request.tenant_name)
            auth_request.tenant_id = dtenant.id
        elif auth_request.tenant_id:
            dtenant = self.__validate_tenant_by_id(auth_request.tenant_id)

        user = api.USER.get_by_name(auth_request.username)
        if not user:
            raise fault.UnauthorizedFault("Unauthorized")

        return self._authenticate(
            validate, user.id, auth_request.tenant_id)

    def authenticate_with_unscoped_token(self, auth_request):
        # Check auth_with_unscoped_token
        if not isinstance(auth_request, auth.AuthWithUnscopedToken):
            raise fault.BadRequestFault("Expecting auth_with_unscoped_token!")

        # We *should* check for an unscoped token here, but as long as
        # POST /tokens w/ credentials auto-scopes to User.tenantId, users can't
        # reach this flow.
        # _token, user = self.__validate_unscoped_token(auth_request.token_id)
        _token, user = self.__validate_token(auth_request.token_id)

        if auth_request.tenant_name:
            dtenant = self.__validate_tenant_by_name(auth_request.tenant_name)
            auth_request.tenant_id = dtenant.id
        elif auth_request.tenant_id:
            dtenant = self.__validate_tenant_by_id(auth_request.tenant_id)

        def validate(duser):
            # The user is already authenticated
            return True

        return self._authenticate(validate, user.id, auth_request.tenant_id)

    def authenticate_ec2(self, credentials):
        # Check credentials
        if not isinstance(credentials, auth.Ec2Credentials):
            raise fault.BadRequestFault("Expecting Ec2 Credentials!")

        creds = api.CREDENTIALS.get_by_access(credentials.access)
        if not creds:
            raise fault.UnauthorizedFault("No credentials found for %s"
                                          % credentials.access)

        def validate(duser):
            signer = Signer(creds.secret)
            signature = signer.generate(credentials)
            if signature == credentials.signature:
                return True
            # NOTE(vish): Some libraries don't use the port when signing
            #             requests, so try again without port.
            if ':' in credentials.host:
                hostname, _port = credentials.host.split(":")
                credentials.host = hostname
                signature = signer.generate(credentials)
                return signature == credentials.signature
            return False

        return self._authenticate(validate, creds.user_id, creds.tenant_id)

    def authenticate_s3(self, credentials):
        # Check credentials
        if not isinstance(credentials, auth.S3Credentials):
            raise fault.BadRequestFault("Expecting S3 Credentials!")

        creds = api.CREDENTIALS.get_by_access(credentials.access)
        if not creds:
            raise fault.UnauthorizedFault("No credentials found for %s"
                                          % credentials.access)

        def validate(duser):
            signer = Signer(creds.secret)
            signature = signer.generate(credentials, s3=True)
            if signature == credentials.signature:
                return True
            return False

        return self._authenticate(validate, creds.user_id, creds.tenant_id)

    def _authenticate(self, validate, user_id, tenant_id=None):
        if tenant_id:
            duser = api.USER.get_by_tenant(user_id, tenant_id)
            if duser == None:
                raise fault.UnauthorizedFault("Unauthorized on this tenant")
        else:
            duser = api.USER.get(user_id)
            if duser == None:
                raise fault.UnauthorizedFault("Unauthorized")

        if not duser.enabled:
            raise fault.UserDisabledFault("Your account has been disabled")

        if not validate(duser):
            raise fault.UnauthorizedFault("Unauthorized")

        # use user's default tenant_id if one is not specified
        tenant_id = tenant_id or duser.tenant_id

        # check for an existing token
        dtoken = api.TOKEN.get_for_user_by_tenant(duser.id, tenant_id)

        if not dtoken or dtoken.expires < datetime.now():
            # Create new token
            dtoken = models.Token()
            dtoken.id = str(uuid.uuid4())
            dtoken.user_id = duser.id
            dtoken.tenant_id = tenant_id
            dtoken.expires = datetime.now() + timedelta(days=1)
            api.TOKEN.create(dtoken)

        return self.__get_auth_data(dtoken)

    def validate_token(self, admin_token, token_id, belongs_to=None):
        self.__validate_service_or_keystone_admin_token(admin_token)
        (token, user) = self.__validate_token(token_id, belongs_to, True)
        return self.__get_validate_data(token, user)

    def revoke_token(self, admin_token, token_id):
        self.__validate_admin_token(admin_token)

        dtoken = api.TOKEN.get(token_id)
        if not dtoken:
            raise fault.ItemNotFoundFault("Token not found")

        api.TOKEN.delete(token_id)

    def get_endpoints_for_token(self, admin_token, token_id):
        self.__validate_admin_token(admin_token)

        dtoken = api.TOKEN.get(token_id)
        if not dtoken:
            raise fault.ItemNotFoundFault("Token not found")

        endpoints = api.TENANT.get_all_endpoints(dtoken.tenant_id)

        # For now it's easier to resend the token data as well.
        # Who knows, might be useful and the client can reuse their
        # auth parsing code.
        token = auth.Token(dtoken.expires, dtoken.id, dtoken.tenant_id)
        return auth.AuthData(token, endpoints)

    #
    #   Tenant Operations
    #

    def create_tenant(self, admin_token, tenant):
        self.__validate_admin_token(admin_token)

        if not isinstance(tenant, Tenant):
            raise fault.BadRequestFault("Expecting a Tenant")

        if not tenant.name:
            raise fault.BadRequestFault("Expecting a unique Tenant Name")

        if api.TENANT.get_by_name(tenant.name) != None:
            raise fault.TenantConflictFault(
                "A tenant with that name already exists")

        dtenant = models.Tenant()
        dtenant.name = tenant.name
        dtenant.desc = tenant.description
        dtenant.enabled = tenant.enabled

        dtenant = api.TENANT.create(dtenant)
        tenant.id = dtenant.id
        return tenant

    def get_tenants(self, admin_token, marker, limit, url,
                    is_service_operation=False):
        """Fetch tenants for either an admin or service operation."""
        ts = []

        if is_service_operation:
            # Check regular token validity.
            (_token, user) = self.__validate_token(admin_token, False)

            # Return tenants specific to user
            dtenants = api.TENANT.tenants_for_user_get_page(
                user, marker, limit)
            prev_page, next_page = api.TENANT.\
                tenants_for_user_get_page_markers(user, marker, limit)
        else:
            #Check Admin Token
            (_token, user) = self.__validate_admin_token(admin_token)
            # Return all tenants
            dtenants = api.TENANT.get_page(marker, limit)
            prev_page, next_page = api.TENANT.get_page_markers(marker, limit)

        for dtenant in dtenants:
            ts.append(Tenant(id=dtenant.id, name=dtenant.name,
                description=dtenant.desc, enabled=dtenant.enabled))

        links = []
        if prev_page:
            links.append(atom.Link('prev',
                "%s?'marker=%s&limit=%s'" % (url, prev_page, limit)))
        if next_page:
            links.append(atom.Link('next',
                "%s?'marker=%s&limit=%s'" % (url, next_page, limit)))

        return Tenants(ts, links)

    def get_tenant(self, admin_token, tenant_id):
        self.__validate_admin_token(admin_token)

        dtenant = api.TENANT.get(tenant_id)
        if not dtenant:
            raise fault.ItemNotFoundFault("The tenant could not be found")
        return Tenant(dtenant.id, dtenant.name, dtenant.desc, dtenant.enabled)

    def update_tenant(self, admin_token, tenant_id, tenant):
        self.__validate_admin_token(admin_token)

        if not isinstance(tenant, Tenant):
            raise fault.BadRequestFault("Expecting a Tenant")

        dtenant = api.TENANT.get(tenant_id)
        if dtenant == None:
            raise fault.ItemNotFoundFault("The tenant could not be found")
        values = {'desc': tenant.description, 'enabled': tenant.enabled}
        api.TENANT.update(tenant_id, values)
        tenant = api.TENANT.get(tenant_id)
        return Tenant(tenant.id, tenant.name, tenant.desc, tenant.enabled)

    def delete_tenant(self, admin_token, tenant_id):
        self.__validate_admin_token(admin_token)

        dtenant = api.TENANT.get(tenant_id)
        if dtenant == None:
            raise fault.ItemNotFoundFault("The tenant could not be found")

        if not api.TENANT.is_empty(tenant_id):
            raise fault.ForbiddenFault("You may not delete a tenant that "
                                       "contains get_users")

        api.TENANT.delete(dtenant.id)
        return None

    #
    # Private Operations
    #
    def __get_dauth_data(self, token_id):
        """return token and user object for a token_id"""

        token = None
        user = None
        if token_id:
            token = api.TOKEN.get(token_id)
            if token:
                user = api.USER.get(token.user_id)
        return (token, user)

    #
    #   User Operations
    #
    def create_user(self, admin_token, user):
        self.__validate_admin_token(admin_token)

        self.validate_and_fetch_user_tenant(user.tenant_id)

        if not isinstance(user, User):
            raise fault.BadRequestFault("Expecting a User")

        if user.name is None or not user.name.strip():
            raise fault.BadRequestFault("Expecting a unique username")

        if api.USER.get_by_name(user.name):
            raise fault.UserConflictFault(
                "A user with that name already exists")

        if user.email is not None:
            if len(user.email) == 0:
                raise fault.BadRequestFault("Expecting a email")
            
            if api.USER.get_by_email(user.email):
                raise fault.EmailConflictFault(
                    "A user with that email already exists")

        if user.eppn is not None:
            if len(user.eppn) == 0:
                raise fault.BadRequestFault("Expecting a eppn")

            if api.USER.get_by_eppn(user.eppn):
                raise fault.EppnConflictFault(
                    "A user with that eppn already exists")

        duser = models.User()
        duser.name = user.name
        duser.password = user.password
        duser.email = user.email
        duser.eppn= user.eppn
        duser.enabled = user.enabled
        duser.tenant_id = user.tenant_id
        duser = api.USER.create(duser)
        user.id = duser.id
        return user

    def validate_and_fetch_user_tenant(self, tenant_id):
        if tenant_id:
            dtenant = api.TENANT.get(tenant_id)
            if dtenant == None:
                raise fault.ItemNotFoundFault("The tenant is not found")
            elif not dtenant.enabled:
                raise fault.TenantDisabledFault(
                    "Your account has been disabled")
            return dtenant

    def get_tenant_users(self, admin_token, tenant_id, marker, limit, url):
        self.__validate_token(admin_token, False)

        if tenant_id == None:
            raise fault.BadRequestFault("Expecting a Tenant Id")
        dtenant = api.TENANT.get(tenant_id)
        if dtenant is  None:
            raise fault.ItemNotFoundFault("The tenant not found")
        if not dtenant.enabled:
            raise fault.TenantDisabledFault("Your account has been disabled")
        ts = []
        dtenantusers = api.USER.users_get_by_tenant_get_page(tenant_id, marker,
                                                          limit)


        for dtenantuser in dtenantusers:
            ts.append(User(None, dtenantuser.id, dtenantuser.name, tenant_id,
                           dtenantuser.email, dtenantuser.eppn, dtenantuser.enabled,
                           dtenantuser.tenant_roles if hasattr(dtenantuser,
                                                    "tenant_roles") else None))
        links = []
        if ts.__len__():
            prev, next = api.USER.users_get_by_tenant_get_page_markers(
                    tenant_id, marker, limit)
            if prev:
                links.append(atom.Link('prev', "%s?'marker=%s&limit=%s'" %
                                      (url, prev, limit)))
            if next:
                links.append(atom.Link('next', "%s?'marker=%s&limit=%s'" %
                                      (url, next, limit)))
        return Users(ts, links)

    def get_users(self, admin_token, marker, limit, url):
        self.__validate_admin_token(admin_token)
        ts = []
        dusers = api.USER.users_get_page(marker, limit)
        for duser in dusers:
            ts.append(User(None, duser.id, duser.name, duser.tenant_id,
                                   duser.email, duser.eppn, duser.enabled))
        links = []
        if ts.__len__():
            prev, next = api.USER.users_get_page_markers(marker, limit)
            if prev:
                links.append(atom.Link('prev', "%s?'marker=%s&limit=%s'" %
                                      (url, prev, limit)))
            if next:
                links.append(atom.Link('next', "%s?'marker=%s&limit=%s'" %
                                      (url, next, limit)))
        return Users(ts, links)

    def get_user(self, admin_token, user_id):
        self.__validate_admin_token(admin_token)
        duser = api.USER.get(user_id)
        if not duser:
            raise fault.ItemNotFoundFault("The user could not be found")
        return User_Update(id=duser.id, tenant_id=duser.tenant_id,
                email=duser.email, eppn=duser.eppn, enabled=duser.enabled, name=duser.name)

    def update_user(self, admin_token, user_id, user):
        self.__validate_admin_token(admin_token)

        duser = api.USER.get(user_id)

        if not duser:
            raise fault.ItemNotFoundFault("The user could not be found")

        if not isinstance(user, User):
            raise fault.BadRequestFault("Expecting a User")

        if not user.email:
            raise fault.BadRequestFault("Expecting a Email")

        if user.email != duser.email and \
                api.USER.get_by_email(user.email) is not None:
            raise fault.EmailConflictFault("Email already exists")

        values = {'email': user.email}
        api.USER.update(user_id, values)
        duser = api.USER.user_get_update(user_id)
        return User(duser.password, duser.id, duser.name, duser.tenant_id,
            duser.email, duser.eppn, duser.enabled)

    def set_user_eppn(self, admin_token, user_id, user):
        self.__validate_admin_token(admin_token)

        duser = api.USER.get(user_id)
        if not duser:
            raise fault.ItemNotFoundFault("The user could not be found")

        if not isinstance(user, User):
            raise fault.BadRequestFault("Expecting a User")

        if not user.eppn:
            raise fault.BadRequestFault("Expecting a Eppn")

        if user.eppn != duser.eppn and \
                api.USER.get_by_eppn(user.eppn) is not None:
            raise fault.EppnConflictFault("Eppn already exists")

        values = {'eppn': user.eppn}
        api.USER.update(user_id, values)

        return User_Update(eppn=user.eppn)

    def set_user_password(self, admin_token, user_id, user):
        self.__validate_admin_token(admin_token)

        duser = api.USER.get(user_id)
        if not duser:
            raise fault.ItemNotFoundFault("The user could not be found")

        if not isinstance(user, User):
            raise fault.BadRequestFault("Expecting a User")

        if not user.password:
            raise fault.BadRequestFault("Expecting a Password")

        values = {'password': user.password}

        api.USER.update(user_id, values)

        return User_Update(password=user.password)

    def enable_disable_user(self, admin_token, user_id, user):
        self.__validate_admin_token(admin_token)
        duser = api.USER.get(user_id)
        if not duser:
            raise fault.ItemNotFoundFault("The user could not be found")
        if not isinstance(user, User):
            raise fault.BadRequestFault("Expecting a User")

        values = {'enabled': user.enabled}

        api.USER.update(user_id, values)

        duser = api.USER.get(user_id)

        return User_Update(enabled=user.enabled)

    def set_user_tenant(self, admin_token, user_id, user):
        self.__validate_admin_token(admin_token)
        duser = api.USER.get(user_id)
        if not duser:
            raise fault.ItemNotFoundFault("The user could not be found")
        if not isinstance(user, User):
            raise fault.BadRequestFault("Expecting a User")

        duser = api.USER.get(user_id)
        if duser == None:
            raise fault.ItemNotFoundFault("The user could not be found")

        if not user.tenant_id:
            raise fault.BadRequestFault("Expecting a TenantID")

        self.validate_and_fetch_user_tenant(user.tenant_id)
        values = {'tenant_id': user.tenant_id}
        api.USER.update(user_id, values)
        return User_Update(tenant_id=user.tenant_id)

    def delete_user(self, admin_token, user_id):
        self.__validate_admin_token(admin_token)
        duser = api.USER.get(user_id)
        if not duser:
            raise fault.ItemNotFoundFault("The user could not be found")

        dtenant = api.TENANT.get(duser.tenant_id)
        if dtenant != None:
            api.USER.delete_tenant_user(user_id, dtenant.id)
        else:
            api.USER.delete(user_id)
        return None

    def __get_auth_data(self, dtoken):
        """return AuthData object for a token"""
        tenant = None
        endpoints = None

        if dtoken.tenant_id:
            dtenant = api.TENANT.get(dtoken.tenant_id)
            tenant = auth.Tenant(id=dtenant.id, name=dtenant.name)

            endpoints = api.TENANT.get_all_endpoints(dtoken.tenant_id)

        token = auth.Token(dtoken.expires, dtoken.id, tenant)

        duser = api.USER.get(dtoken.user_id)

        ts = []
        if dtoken.tenant_id:
            drole_refs = api.ROLE.ref_get_all_tenant_roles(duser.id,
                dtoken.tenant_id)
            for drole_ref in drole_refs:
                drole = api.ROLE.get(drole_ref.role_id)
                ts.append(UserRole(drole_ref.role_id, drole.name,
                    drole_ref.tenant_id))
        drole_refs = api.ROLE.ref_get_all_global_roles(duser.id)
        for drole_ref in drole_refs:
            drole = api.ROLE.get(drole_ref.role_id)
            ts.append(UserRole(drole_ref.role_id, drole.name,
                drole_ref.tenant_id))

        user = auth.User(duser.id, duser.name, None, None, UserRoles(ts, []))

        return auth.AuthData(token, user, endpoints)

    def __get_validate_data(self, dtoken, duser):
        """return ValidateData object for a token/user pair"""
        tenant = None
        if dtoken.tenant_id:
            dtenant = api.TENANT.get(dtoken.tenant_id)
            tenant = auth.Tenant(id=dtenant.id, name=dtenant.name)

        token = auth.Token(dtoken.expires, dtoken.id, tenant)

        ts = []
        if dtoken.tenant_id:
            drole_refs = api.ROLE.ref_get_all_tenant_roles(duser.id,
                dtoken.tenant_id)
            for drole_ref in drole_refs:
                drole = api.ROLE.get(drole_ref.role_id)
                ts.append(UserRole(drole_ref.role_id, drole.name,
                    drole_ref.tenant_id))
        drole_refs = api.ROLE.ref_get_all_global_roles(duser.id)
        for drole_ref in drole_refs:
            drole = api.ROLE.get(drole_ref.role_id)
            ts.append(UserRole(drole_ref.role_id, drole.name,
                drole_ref.tenant_id))

        # Also get the user's tenant's name
        tenant_name = None
        if duser.tenant_id:
            utenant = api.TENANT.get(duser.tenant_id)
            tenant_name = utenant.name

        user = auth.User(duser.id, duser.name, duser.tenant_id,
            tenant_name, UserRoles(ts, []))

        return auth.ValidateData(token, user)

    def __validate_tenant(self, dtenant):
        if not dtenant:
            raise fault.UnauthorizedFault("Tenant not found")

        if not dtenant.enabled:
            raise fault.TenantDisabledFault("Tenant %s has been disabled!"
                % dtenant.id)

        return dtenant

    def __validate_tenant_by_id(self, tenant_id):
        if not tenant_id:
            raise fault.UnauthorizedFault("Missing tenant id")

        dtenant = api.TENANT.get(tenant_id)

        return self.__validate_tenant(dtenant)

    def __validate_tenant_by_name(self, tenant_name):
        if not tenant_name:
            raise fault.UnauthorizedFault("Missing tenant name")

        dtenant = api.TENANT.get_by_name(name=tenant_name)

        return self.__validate_tenant(dtenant)

    def __validate_token(self, token_id, belongs_to=None, is_check_token=None):
        """
        Method to validate a token.
        token_id -- value of actual token that need to be validated.
        belngs_to -- optional tenant_id to check whether the token is
        mapped to a specific tenant.
        is_check_token -- optional argument that tells whether
        we check the existence of a Token using another Token
        to authenticate.This value decides the faults that are to be thrown.
        """
        if not token_id:
            raise fault.UnauthorizedFault("Missing token")

        (token, user) = self.__get_dauth_data(token_id)

        if not token:
            if is_check_token:
                raise fault.ItemNotFoundFault("Token does not exist.")
            else:
                raise fault.UnauthorizedFault(
                    "Bad token, please reauthenticate")

        if token.expires < datetime.now():
            if is_check_token:
                raise fault.ItemNotFoundFault("Token expired, please renew.")
            else:
                raise fault.ForbiddenFault("Token expired, please renew.")

        if not user.enabled:
            raise fault.UserDisabledFault("User %s has been disabled!"
                % user.id)

        if user.tenant_id:
            self.__validate_tenant_by_id(user.tenant_id)

        if token.tenant_id:
            self.__validate_tenant_by_id(token.tenant_id)

        if belongs_to and unicode(token.tenant_id) != unicode(belongs_to):
            raise fault.UnauthorizedFault("Unauthorized on this tenant")

        return (token, user)

    def __validate_unscoped_token(self, token_id, belongs_to=None):
        (token, user) = self.__validate_token(token_id, belongs_to)

        if token.tenant_id:
            raise fault.ForbiddenFault("Expecting unscoped token")

        return (token, user)

    def __validate_admin_token(self, token_id):
        (token, user) = self.__validate_token(token_id)

        if backends.ADMIN_ROLE_ID is None:
            role = api.ROLE.get_by_name(backends.ADMIN_ROLE_NAME)
            backends.ADMIN_ROLE_ID = role.id

        for role_ref in api.ROLE.ref_get_all_global_roles(user.id):
            if role_ref.role_id == backends.ADMIN_ROLE_ID and \
                    role_ref.tenant_id is None:
                return (token, user)

        raise fault.UnauthorizedFault(
            "You are not authorized to make this call")

    def __validate_service_or_keystone_admin_token(self, token_id):
        (token, user) = self.__validate_token(token_id)

        if backends.ADMIN_ROLE_ID is None:
            role = api.ROLE.get_by_name(backends.ADMIN_ROLE_NAME)
            if role:
                backends.ADMIN_ROLE_ID = role.id
            else:
                LOG.error('Admin role is missing.')

        if backends.SERVICE_ADMIN_ROLE_ID is None:
            role = api.ROLE.get_by_name(backends.SERVICE_ADMIN_ROLE_NAME)
            if role:
                backends.SERVICE_ADMIN_ROLE_ID = role.id
            else:
                LOG.warn('No service admin role is defined.')

        for role_ref in api.ROLE.ref_get_all_global_roles(user.id):
            if (role_ref.role_id == backends.ADMIN_ROLE_ID or \
                role_ref.role_id == backends.SERVICE_ADMIN_ROLE_ID) \
                and role_ref.tenant_id is None:
                return (token, user)

        raise fault.UnauthorizedFault(
            "You are not authorized to make this call")

    def create_role(self, admin_token, role):
        self.__validate_service_or_keystone_admin_token(admin_token)

        if not isinstance(role, Role):
            raise fault.BadRequestFault("Expecting a Role")

        if not role.name:
            raise fault.BadRequestFault("Expecting a Role name")

        if api.ROLE.get(role.name) != None:
            raise fault.RoleConflictFault(
                "A role with that name '" + role.name + "' already exists")
        #Check if the passed service exist
        #and the role begins with service_id:.
        if role.service_id:
            service = api.SERVICE.get(role.service_id)
            if service is None:
                raise fault.BadRequestFault(
                    "A service with that id doesnt exist.")
            if not role.name.startswith(service.name + ":"):
                raise fault.BadRequestFault(
                    "Role should begin with service name '" +
                        service.name + ":'")

        drole = models.Role()
        drole.name = role.name
        drole.desc = role.description
        drole.service_id = role.service_id
        drole = api.ROLE.create(drole)
        role.id = drole.id
        return role

    def get_roles(self, admin_token, marker, limit, url):
        self.__validate_service_or_keystone_admin_token(admin_token)

        ts = []
        droles = api.ROLE.get_page(marker, limit)
        for drole in droles:
            ts.append(Role(drole.id, drole.name, drole.desc, drole.service_id))
        prev, next = api.ROLE.get_page_markers(marker, limit)
        links = []
        if prev:
            links.append(atom.Link('prev', "%s?'marker=%s&limit=%s'" \
                                                % (url, prev, limit)))
        if next:
            links.append(atom.Link('next', "%s?'marker=%s&limit=%s'" \
                                                % (url, next, limit)))
        return Roles(ts, links)

    def get_role(self, admin_token, role_id):
        self.__validate_service_or_keystone_admin_token(admin_token)

        drole = api.ROLE.get(role_id)
        if not drole:
            raise fault.ItemNotFoundFault("The role could not be found")
        return Role(drole.id, drole.name, drole.desc, drole.service_id)

    def delete_role(self, admin_token, role_id):
        self.__validate_service_or_keystone_admin_token(admin_token)
        drole = api.ROLE.get(role_id)
        if not drole:
            raise fault.ItemNotFoundFault("The role could not be found")
        role_refs = api.ROLE.ref_get_by_role(role_id)
        if role_refs != None:
            for role_ref in role_refs:
                api.ROLE.ref_delete(role_ref.id)
        api.ROLE.delete(role_id)

    def create_role_ref(self, admin_token, user_id, role_ref):
        self.__validate_service_or_keystone_admin_token(admin_token)
        duser = api.USER.get(user_id)

        if not duser:
            raise fault.ItemNotFoundFault("The user could not be found")

        if not isinstance(role_ref, RoleRef):
            raise fault.BadRequestFault("Expecting a Role Ref")

        if role_ref.role_id == None:
            raise fault.BadRequestFault("Expecting a Role Id")

        drole = api.ROLE.get(role_ref.role_id)
        if drole == None:
            raise fault.ItemNotFoundFault("The role not found")

        if role_ref.tenant_id != None:
            dtenant = api.TENANT.get(role_ref.tenant_id)
            if dtenant == None:
                raise fault.ItemNotFoundFault("The tenant not found")

        drole_ref = models.UserRoleAssociation()
        drole_ref.user_id = duser.id
        drole_ref.role_id = drole.id
        if role_ref.tenant_id != None:
            drole_ref.tenant_id = dtenant.id
        user_role_ref = api.USER.user_role_add(drole_ref)
        role_ref.role_ref_id = user_role_ref.id
        return role_ref

    def delete_role_ref(self, admin_token, role_ref_id):
        self.__validate_service_or_keystone_admin_token(admin_token)
        api.ROLE.ref_delete(role_ref_id)
        return None

    def add_global_role_to_user(self, admin_token, user_id, role_id):
        self.__validate_service_or_keystone_admin_token(admin_token)
        duser = api.USER.get(user_id)
        if not duser:
            raise fault.ItemNotFoundFault("The user could not be found")

        drole = api.ROLE.get(role_id)
        if drole == None:
            raise fault.ItemNotFoundFault("The role not found")

        drole_ref = models.UserRoleAssociation()
        drole_ref.user_id = duser.id
        drole_ref.role_id = drole.id
        api.USER.user_role_add(drole_ref)

    def get_user_roles(self, admin_token, marker, limit, url, user_id):
        self.__validate_service_or_keystone_admin_token(admin_token)
        duser = api.USER.get(user_id)

        if not duser:
            raise fault.ItemNotFoundFault("The user could not be found")

        ts = []
        drole_refs = api.ROLE.ref_get_page(marker, limit, user_id)
        for drole_ref in drole_refs:
            ts.append(RoleRef(drole_ref.id, drole_ref.role_id,
                                     drole_ref.tenant_id))
        prev, next = api.ROLE.ref_get_page_markers(user_id, marker, limit)
        links = []
        if prev:
            links.append(atom.Link('prev',
                "%s?'marker=%s&limit=%s'" % (url, prev, limit)))
        if next:
            links.append(atom.Link('next',
                "%s?'marker=%s&limit=%s'" % (url, next, limit)))
        return RoleRefs(ts, links)

    def add_endpoint_template(self, admin_token, endpoint_template):
        self.__validate_service_or_keystone_admin_token(admin_token)

        if not isinstance(endpoint_template, EndpointTemplate):
            raise fault.BadRequestFault("Expecting a EndpointTemplate")

        if endpoint_template.service == None or \
            len(endpoint_template.service.strip()) == 0:
            raise fault.BadRequestFault(
                    "Expecting serviceId.")
        if endpoint_template.service != None and\
            len(endpoint_template.service.strip()) > 0 and\
            api.SERVICE.get(endpoint_template.service) == None:
            raise fault.BadRequestFault(
                    "A service with that id doesn't exist.")
        dendpoint_template = models.EndpointTemplates()
        dendpoint_template.region = endpoint_template.region
        dendpoint_template.service_id = endpoint_template.service
        dendpoint_template.public_url = endpoint_template.public_url
        dendpoint_template.admin_url = endpoint_template.admin_url
        dendpoint_template.internal_url = endpoint_template.internal_url
        dendpoint_template.enabled = endpoint_template.enabled
        dendpoint_template.is_global = endpoint_template.is_global
        dendpoint_template = api.ENDPOINT_TEMPLATE.create(dendpoint_template)
        endpoint_template.id = dendpoint_template.id
        return endpoint_template

    def modify_endpoint_template(self,
        admin_token, endpoint_template_id, endpoint_template):
        self.__validate_service_or_keystone_admin_token(admin_token)

        if not isinstance(endpoint_template, EndpointTemplate):
            raise fault.BadRequestFault("Expecting a EndpointTemplate")
        dendpoint_template = api.ENDPOINT_TEMPLATE.get(endpoint_template_id)
        if not dendpoint_template:
            raise fault.ItemNotFoundFault(
                "The endpoint template could not be found")

        #Check if the passed service exist.
        if endpoint_template.service != None and\
            len(endpoint_template.service.strip()) > 0 and\
            api.SERVICE.get(endpoint_template.service) == None:
            raise fault.BadRequestFault(
                    "A service with that id doesn't exist.")
        dendpoint_template.region = endpoint_template.region
        dendpoint_template.service_id = endpoint_template.service
        dendpoint_template.public_url = endpoint_template.public_url
        dendpoint_template.admin_url = endpoint_template.admin_url
        dendpoint_template.internal_url = endpoint_template.internal_url
        dendpoint_template.enabled = endpoint_template.enabled
        dendpoint_template.is_global = endpoint_template.is_global
        dendpoint_template = api.ENDPOINT_TEMPLATE.update(
            endpoint_template_id, dendpoint_template)
        return EndpointTemplate(
            dendpoint_template.id,
            dendpoint_template.region,
            dendpoint_template.service_id,
            dendpoint_template.public_url,
            dendpoint_template.admin_url,
            dendpoint_template.internal_url,
            dendpoint_template.enabled,
            dendpoint_template.is_global)

    def delete_endpoint_template(self, admin_token, endpoint_template_id):
        self.__validate_service_or_keystone_admin_token(admin_token)
        dendpoint_template = api.ENDPOINT_TEMPLATE.get(endpoint_template_id)
        if not dendpoint_template:
            raise fault.ItemNotFoundFault(
                "The endpoint template could not be found")
        #Delete Related endpoints
        endpoints = api.ENDPOINT_TEMPLATE.\
            endpoint_get_by_endpoint_template(endpoint_template_id)
        if endpoints != None:
            for endpoint in endpoints:
                api.ENDPOINT_TEMPLATE.endpoint_delete(endpoint.id)
        api.ENDPOINT_TEMPLATE.delete(endpoint_template_id)

    def get_endpoint_templates(self, admin_token, marker, limit, url):
        self.__validate_service_or_keystone_admin_token(admin_token)

        ts = []
        dendpoint_templates = api.ENDPOINT_TEMPLATE.get_page(marker, limit)
        for dendpoint_template in dendpoint_templates:
            ts.append(EndpointTemplate(
                dendpoint_template.id,
                dendpoint_template.region,
                dendpoint_template.service_id,
                dendpoint_template.public_url,
                dendpoint_template.admin_url,
                dendpoint_template.internal_url,
                dendpoint_template.enabled,
                dendpoint_template.is_global))
        prev, next = api.ENDPOINT_TEMPLATE.get_page_markers(marker, limit)
        links = []
        if prev:
            links.append(atom.Link('prev', "%s?'marker=%s&limit=%s'" \
                                                % (url, prev, limit)))
        if next:
            links.append(atom.Link('next', "%s?'marker=%s&limit=%s'" \
                                                % (url, next, limit)))
        return EndpointTemplates(ts, links)

    def get_endpoint_template(self, admin_token, endpoint_template_id):
        self.__validate_service_or_keystone_admin_token(admin_token)

        dendpoint_template = api.ENDPOINT_TEMPLATE.get(endpoint_template_id)
        if not dendpoint_template:
            raise fault.ItemNotFoundFault(
                "The endpoint template could not be found")
        return EndpointTemplate(
            dendpoint_template.id,
            dendpoint_template.region,
            dendpoint_template.service_id,
            dendpoint_template.public_url,
            dendpoint_template.admin_url,
            dendpoint_template.internal_url,
            dendpoint_template.enabled,
            dendpoint_template.is_global)

    def get_tenant_endpoints(self, admin_token, marker, limit, url, tenant_id):
        self.__validate_service_or_keystone_admin_token(admin_token)
        if tenant_id == None:
            raise fault.BadRequestFault("Expecting a Tenant Id")

        if api.TENANT.get(tenant_id) == None:
            raise fault.ItemNotFoundFault("The tenant not found")

        ts = []

        dtenant_endpoints = \
            api.ENDPOINT_TEMPLATE.\
                endpoint_get_by_tenant_get_page(
                    tenant_id, marker, limit)
        for dtenant_endpoint in dtenant_endpoints:
            ts.append(Endpoint(dtenant_endpoint.id,
                    url + '/endpointTemplates/' + \
                    str(dtenant_endpoint.endpoint_template_id)))
        links = []
        if ts.__len__():
            prev, next = \
                api.ENDPOINT_TEMPLATE.endpoint_get_by_tenant_get_page_markers(
                    tenant_id, marker, limit)
            if prev:
                links.append(atom.Link('prev', "%s?'marker=%s&limit=%s'" %
                                      (url, prev, limit)))
            if next:
                links.append(atom.Link('next', "%s?'marker=%s&limit=%s'" %
                                      (url, next, limit)))
        return Endpoints(ts, links)

    def create_endpoint_for_tenant(self, admin_token,
                                     tenant_id, endpoint_template, url):
        self.__validate_service_or_keystone_admin_token(admin_token)
        if tenant_id == None:
            raise fault.BadRequestFault("Expecting a Tenant Id")
        if api.TENANT.get(tenant_id) == None:
            raise fault.ItemNotFoundFault("The tenant not found")

        dendpoint_template = api.ENDPOINT_TEMPLATE.get(endpoint_template.id)
        if not dendpoint_template:
            raise fault.ItemNotFoundFault(
                "The endpoint template could not be found")
        dendpoint = models.Endpoints()
        dendpoint.tenant_id = tenant_id
        dendpoint.endpoint_template_id = endpoint_template.id
        dendpoint = api.ENDPOINT_TEMPLATE.endpoint_add(dendpoint)
        dendpoint = Endpoint(dendpoint.id, url +
            '/endpointTemplates/' + unicode(dendpoint.endpoint_template_id))
        return dendpoint

    def delete_endpoint(self, admin_token, endpoint_id):
        self.__validate_service_or_keystone_admin_token(admin_token)
        if api.ENDPOINT_TEMPLATE.get(endpoint_id) == None:
            raise fault.ItemNotFoundFault("The Endpoint is not found.")
        api.ENDPOINT_TEMPLATE.endpoint_delete(endpoint_id)
        return None

    #Service Operations
    def create_service(self, admin_token, service):
        self.__validate_service_or_keystone_admin_token(admin_token)

        if not isinstance(service, Service):
            raise fault.BadRequestFault("Expecting a Service")

        if service.name == None:
            raise fault.BadRequestFault("Expecting a Service Name")

        if api.SERVICE.get_by_name(service.name) != None:
            raise fault.ServiceConflictFault(
                "A service with that name already exists")

        dservice = models.Service()
        dservice.name = service.name
        dservice.type = service.type
        dservice.desc = service.description
        dservice = api.SERVICE.create(dservice)
        service.id = dservice.id
        return service

    def get_services(self, admin_token, marker, limit, url):
        self.__validate_service_or_keystone_admin_token(admin_token)

        ts = []
        dservices = api.SERVICE.get_page(marker, limit)
        for dservice in dservices:
            ts.append(Service(dservice.id, dservice.name, dservice.type,
                dservice.desc))
        prev, next = api.SERVICE.get_page_markers(marker, limit)
        links = []
        if prev:
            links.append(atom.Link('prev', "%s?'marker=%s&limit=%s'" \
                                                % (url, prev, limit)))
        if next:
            links.append(atom.Link('next', "%s?'marker=%s&limit=%s'" \
                                                % (url, next, limit)))
        return Services(ts, links)

    def get_service(self, admin_token, service_id):
        self.__validate_service_or_keystone_admin_token(admin_token)

        dservice = api.SERVICE.get(service_id)
        if not dservice:
            raise fault.ItemNotFoundFault("The service could not be found")
        return Service(dservice.id, dservice.name, dservice.type,
            dservice.desc)

    def delete_service(self, admin_token, service_id):
        self.__validate_service_or_keystone_admin_token(admin_token)
        dservice = api.SERVICE.get(service_id)

        if not dservice:
            raise fault.ItemNotFoundFault("The service could not be found")

        #Delete Related Endpointtemplates and Endpoints.
        endpoint_templates = api.ENDPOINT_TEMPLATE.get_by_service(service_id)
        if endpoint_templates != None:
            for endpoint_template in endpoint_templates:
                endpoints = api.ENDPOINT_TEMPLATE.\
                    endpoint_get_by_endpoint_template(endpoint_template.id)
                if endpoints != None:
                    for endpoint in endpoints:
                        api.ENDPOINT_TEMPLATE.endpoint_delete(endpoint.id)
                api.ENDPOINT_TEMPLATE.delete(endpoint_template.id)
        #Delete Related Role and RoleRefs
        roles = api.ROLE.get_by_service(service_id)
        if roles != None:
            for role in roles:
                role_refs = api.ROLE.ref_get_by_role(role.id)
                if role_refs != None:
                    for role_ref in role_refs:
                        api.ROLE.ref_delete(role_ref.id)
                api.ROLE.delete(role.id)
        api.SERVICE.delete(service_id)

    '''
    get token by email
    add by colony. 
    '''
    def get_token_by_email(self, admin_token, email):
        self.__validate_admin_token(admin_token)
        dmail = api.USER.get_by_email(email)

        def validate(duser):
            # The user is already authenticated by gakunin
            return True

        if dmail:
            return self._authenticate(validate, dmail.id)
        raise fault.ItemNotFoundFault("email not found")

    def get_token_by_eppn(self, admin_token, eppn):
        self.__validate_admin_token(admin_token)
        deppn = api.USER.get_by_eppn(eppn)

        def validate(duser):
            # The user is already authenticated by gakunin
            return True

        if deppn:
            return self._authenticate(validate, deppn.id)
        raise fault.ItemNotFoundFault("eppn not found")
