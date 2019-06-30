"""Kong Configurator Certbot plugins.
"""
import logging
import pickle

import zope.interface

import certbot.constants
from certbot import errors
from certbot import interfaces
from certbot import util
from certbot.compat import misc
from certbot.compat import os
from certbot.plugins import common

from certbot_kong.kong_admin_api import KongAdminApi
from certbot_kong.kong_config import Config
from certbot_kong import constants

logger = logging.getLogger(__name__)

@zope.interface.implementer(interfaces.IInstaller)
@zope.interface.provider(interfaces.IPluginFactory)
class KongConfigurator(common.Installer):
    """Kong Configurator.
    .. todo:: Add interfaces.IAuthenticator functionality
    :ivar str save_notes: Human-readable config change notes
    """

    description = "Kong Configurator"

    @classmethod
    def add_parser_arguments(cls, add):
        add("admin-url", default=constants.CLI_DEFAULTS["admin_url"],
            help="kong admin URL.")
        add("delete-unused-certificates", default=True,
            help="Delete certificates when it no longer references any SNIs")   
        add("redirect-route-no-host", default=True,
            help="Include redirect HTTP to HTTPS for routes which do not "
            "specify any hosts")
        add("redirect-route-any-host", default=True,
            help="Include redirect HTTP to HTTPS for routes which has at "
            "least one host which matches the domain")

    def __init__(self, *args, **kwargs):
        # TODO add enable redirect enhancement. 
        # Impacted kong routes need protocol set to ["HTTPS"]
        # i.e. no HTTP 
        #self._enhance_func = {"redirect": self._enable_redirect}
        super(KongConfigurator, self).__init__(*args, **kwargs)
        self._enhance_func = {"redirect": self._enable_redirect}
        
        self.save_notes = ""

    def prepare(self):
        """Prepare the authenticator/installer.
        """

        self._api = KongAdminApi(
            url=self.conf('admin-url'))

        self._config = Config(self._api)

    def _enable_redirect(self, domain, unused_options):
        """Redirect HTTP traffic to HTTPS for routes matching domain.
        .. note:: This function saves the configuration
        :param str domain: domain to enable redirect for
        :param unused_options: Not currently used
        :type unused_options: Not Available
        """

        for route in self._config.routes:
            hosts = route.get('hosts',[])
            protocols = route.get('protocols',[])

            if 'http' not in protocols:
                # route already redircting
                continue 
            
            if hosts:
                if(self._is_wildcard_domain(domain)):
                    matched_hosts = self._determine_matched_domains(
                        domain, hosts)
                    
                    if(len(matched_hosts)>0):
                        if(len(matched_hosts) == len(hosts) or
                                self.conf('redirect-route-any-host')):
                            self._config.redirect_route(route['id'])
                else:
                    if(
                        (len(hosts) == 1 and domain == hosts[0]) or
                        (
                            self.conf('redirect-route-any-host') and 
                            domain in hosts
                        )
                    ):
                        self._config.redirect_route(route['id'])
            else:
                if self.conf('redirect-route-no-host'):
                    self._config.redirect_route(route['id'])

        
        self.save()

    def get_all_names(self):  # type: ignore
        """Returns all names found in the Kong Configuration.
        :returns: all the hosts from all the routes and all the snis from certificates
        :rtype: set
        """
        all_names = set()  # type: Set[str]

        for c in self._config.certs:
            snis = c.get('snis', [])
            for sni in snis:
                all_names.add(sni)

        for r in self._config.routes:
            hosts = r.get('hosts', [])
            for host in hosts:
                all_names.add(host)

        return all_names

    def deploy_cert(self, domain, cert_path, key_path, chain_path, fullchain_path):
        """Deploy certificate.
        :param str domain: domain to deploy certificate file
        :param str cert_path: absolute path to the certificate file
        :param str key_path: absolute path to the private key file
        :param str chain_path: absolute path to the certificate chain file
        :param str fullchain_path: absolute path to the certificate fullchain
            file (cert plus chain)
        :raises .PluginError: when cert cannot be deployed
        """
        if not fullchain_path:
            raise errors.PluginError(
                "The kong plugin requires --fullchain-path to "
                "install a cert.")
        
        domains = []
        if self._is_wildcard_domain(domain):
            domains = self._determine_domains(domain)
        else:
            domains = [domain]

        if len(domains) == 0:
            logger.info("No route hosts matching %s",
                domain)
            return
        
        try:
            key_str = None
            with open(key_path, 'r') as file:
                key_str = file.read()

            fullchain_str = None
            with open(fullchain_path, 'r') as file:
                fullchain_str = file.read()
        except (IOError):
            logger.debug('Encountered error:', exc_info=True)
            raise errors.PluginError('Unable to open cert files.')

        for d in domains:
            self._config.set_sni_cert(d, fullchain_str, key_str, 
                self.conf('delete-unused-certificates'))
            self.save_notes = "\n".join(self._config.get_changes_details())

    def _is_wildcard_domain(self, domain):
        """ helper method to determine whether a domain is wildcard domain.
        *.example.com is 
        www.example.com is not
        www.example.* is not
        *ww.exmape.com is not
        """
        if domain.startswith('*.') and len(domain.split('.')) > 2:
            return True
        else: 
            return False

    def _determine_domains(self, wildcard_domain):
        """ helper method to find all route hosts matching a wildcard domain.
        """
        domains = set()
        for r in self._config.routes:
            domains.update(r.get('hosts',[]))
        
        for c in self._config.certs:
            domains.update(c.get('snis',[]))

        return self._determine_matched_domains(wildcard_domain, 
            list(domains))

    def _determine_matched_domains(self, wildcard_domain, domains):
        matched_domains = []
        for d in domains:
            if self._matched_domain(d, wildcard_domain):
                matched_domains.append(d)

        return matched_domains
    
    def _matched_domain( self,
            domain, # type: str
            wildcard_domain # type: str
            ):
        """ helper function to determine if the host matches 
        the wildcard domain.
        """

        # split and remove the subdomain
        domain_components = domain.split('.')[1:]
        if len(domain_components) < 1:
            return False

        # split and remove the "*" segment
        wildcard_domain_components = wildcard_domain.split('.')[1:] 

        # can now compare for a match
        if domain_components == wildcard_domain_components:
            return True
        else: 
            return False


    def enhance(self, domain, enhancement, options=None):
        """Perform a configuration enhancement.
        :param str domain: domain for which to provide enhancement
        :param str enhancement: An enhancement as defined in
            :const:`~certbot.constants.ENHANCEMENTS`
        :param options: Flexible options parameter for enhancement.
            Check documentation of
            :const:`~certbot.constants.ENHANCEMENTS`
            for expected options for each enhancement.
        :raises .PluginError: If Enhancement is not supported, or if
            an error occurs during the enhancement.
        """
        try:
            return self._enhance_func[enhancement](domain, options)
        except (KeyError, ValueError):
            raise errors.PluginError(
                "Unsupported enhancement: {0}".format(enhancement))
        except errors.PluginError:
            logger.warning("Failed %s for %s", enhancement, domain)
            raise

    def supported_enhancements(self):  # type: ignore
        """Returns a `collections.Iterable` of supported enhancements.
        :returns: supported enhancements which should be a subset of
            :const:`~certbot.constants.ENHANCEMENTS`
        :rtype: :class:`collections.Iterable` of :class:`str`
        """
        return self._enhance_func.keys()

    def save(self, title=None, temporary=False):
        """Saves all changes to the configuration files.
        Both title and temporary are needed because a save may be
        intended to be permanent, but the save is not ready to be a full
        checkpoint.
        It is assumed that at most one checkpoint is finalized by this
        method. Additionally, if an exception is raised, it is assumed a
        new checkpoint was not finalized.
        :param str title: The title of the save. If a title is given, the
            configuration will be saved as a new checkpoint and put in a
            timestamped directory. `title` has no effect if temporary is true.
        :param bool temporary: Indicates whether the changes made will
            be quickly reversed in the future (challenges)
        :raises .PluginError: when save is unsuccessful
        """
        try:
            self.save_notes = "\n".join(self._config.get_changes_details())
            self._config.apply_changes()
            conf_dump_filename = self._get_conf_dump_filename()
            self._dump_config(conf_dump_filename)
            self.add_to_checkpoint([conf_dump_filename], self.save_notes, temporary)

            self._config.clear_changes()
            self.save_notes = ""
            self._config.load_config()
            
            if title and not temporary:
                self.finalize_checkpoint(title)
        except:
            raise errors.PluginError("Unable to apply changes")
        

    def _get_conf_dump_filename(self):
        return os.path.join(self.config.work_dir,"kong_conf")

    def more_info(self):
        """Human-readable string to help understand the module"""
        return (
            "Configures Kong to install certificates"
        )

    def rollback_checkpoints(self, rollback=1):
        """Revert `rollback` number of configuration checkpoints.
        :raises .PluginError: when configuration cannot be fully reverted
        """
        super(KongConfigurator, self).rollback_checkpoints(rollback)
        conf_dump_filename = self._get_conf_dump_filename()
        self._load_config(conf_dump_filename)
        self._config.undo_changes()
        self._config.clear_changes()
        self.save_notes = ""
        self._config.load_config()

    def recovery_routine(self):  # type: ignore
        """Revert configuration to most recent finalized checkpoint.
        Remove all changes (temporary and permanent) that have not been
        finalized. This is useful to protect against crashes and other
        execution interruptions.
        :raises .errors.PluginError: If unable to recover the configuration
        """

        super(KongConfigurator, self).recovery_routine()
        conf_dump_filename = self._get_conf_dump_filename()
        self._load_config(conf_dump_filename)
        self._config.undo_changes()
        self._config.clear_changes()
        self.save_notes = ""
        self._config.load_config()

    def config_test(self):
        """Not required for Kong. Config is always valid"""
        pass

    def restart(self):
        """Not required for Kong. No restart required to apply configurations"""
        pass

    def _dump_config(self, filename):
        with open(filename, 'wb') as f:
            pickle.dump(self._config, f, pickle.HIGHEST_PROTOCOL)
    
    def _load_config(self, filename):
        with open(filename, 'rb') as f:
            self._config =  pickle.load(f)

