# -*- coding: utf-8 -*-
import hashlib
import base64
import textwrap
import datetime
import random

import Crypto.Util.number
import stem
from keycity import Keycity

from onionbalance import util
from onionbalance import log
from onionbalance import config

logger = log.get_logger()


def choose_introduction_point_set(available_introduction_points):
    """
    Select a set introduction points to included in a HS descriptor.

    Provided with a list of available introduction points for each
    backend instance for an onionbalance service.

    Introduciton points are selected to try and achieve the greatest
    distribution of introduction points across all of the available backend
    instances.

    Return a list of IntroductionPoints.
    """

    # Shuffle the instance order before beginning to pick intro points
    random.shuffle(available_introduction_points)

    num_active_instances = len(available_introduction_points)
    ips_per_instance = [len(ips) for ips in available_introduction_points]
    num_intro_points = sum(ips_per_instance)

    # Choose up to `MAX_INTRO_POINTS` IPs from the service instances. If less
    # than `MAX_INTRO_POINTS` IPs are available, we should pick all available
    # IP's
    max_introduction_points = min(num_intro_points,
                                  config.MAX_INTRO_POINTS)

    # Determine the maximum number of IP's which can be selected from
    # each instance to give the widest distribution of introduction
    # point
    pos = 0
    intro_selection = [0] * num_active_instances

    # Keep looping until we have selected enough introduction points
    while sum(intro_selection) < max_introduction_points:
        # Check if the current instance has more IPs available
        if(ips_per_instance[pos] - intro_selection[pos] > 0):
            intro_selection[pos] += 1
        # Increment and wrap the pointer to the current instance
        pos = ((pos + 1) % num_active_instances)

    # intro_selection now lists the count/number of IPs to select from each
    # instance. We now sample the determined number of IPs from the IPs
    # available for each instance.
    choosen_intro_points = []
    for count, intros in zip(intro_selection, available_introduction_points):
        choosen_intro_points.extend(random.sample(intros, count))

    # Shuffle choosen IP's to try reveal less information about which
    # instances are online and have introduction points included.
    random.shuffle(choosen_intro_points)

    return choosen_intro_points


def generate_service_descriptor(onion_address, introduction_point_list=None,
                                replica=0, timestamp=None, deviation=0):
    """
    High-level interface for generating a signed HS descriptor
    """

    if not timestamp:
        timestamp = datetime.datetime.utcnow()
    unix_timestamp = int(timestamp.strftime("%s"))

    permanent_key_block = make_public_key_block(Keycity().pubkey_please(onion_address))
    permanent_id = util.calc_permanent_id(Keycity().pubkey_please(onion_address))

    # Calculate the current secret-id-part for this hidden service
    # Deviation allows the generation of a descriptor for a different time
    # period.
    time_period = (util.get_time_period(unix_timestamp, permanent_id)
                   + int(deviation))

    secret_id_part = util.calc_secret_id_part(time_period, None, replica)
    descriptor_id = util.calc_descriptor_id(permanent_id, secret_id_part)

    if not introduction_point_list:
        raise ValueError("No introduction points for service %s.onion." %
                         onion_address)

    # Generate the introduction point section of the descriptor
    intro_section = make_introduction_points_part(
        introduction_point_list
    )

    unsigned_descriptor = generate_hs_descriptor_raw(
        desc_id_base32=util.base32_encode_str(descriptor_id),
        permanent_key_block=permanent_key_block,
        secret_id_part_base32=util.base32_encode_str(secret_id_part),
        publication_time=util.rounded_timestamp(timestamp),
        introduction_points_part=intro_section
    )

    signed_descriptor = sign_descriptor(unsigned_descriptor, onion_address)
    return signed_descriptor


def generate_hs_descriptor_raw(desc_id_base32, permanent_key_block,
                               secret_id_part_base32, publication_time,
                               introduction_points_part):
    """
    Generate hidden service descriptor string
    """
    doc = [
        "rendezvous-service-descriptor {}".format(desc_id_base32),
        "version 2",
        "permanent-key",
        permanent_key_block,
        "secret-id-part {}".format(secret_id_part_base32),
        "publication-time {}".format(publication_time),
        "protocol-versions 2,3",
        "introduction-points",
        introduction_points_part,
        "signature\n",
    ]

    unsigned_descriptor = '\n'.join(doc)
    return unsigned_descriptor


def make_introduction_points_part(introduction_point_list=None):
    """
    Make introduction point block from list of IntroductionPoint objects
    """

    # If no intro points were specified, we should create an empty list
    if not introduction_point_list:
        introduction_point_list = []

    intro = []
    for intro_point in introduction_point_list:
        intro.append("introduction-point {}".format(intro_point.identifier))
        intro.append("ip-address {}".format(intro_point.address))
        intro.append("onion-port {}".format(intro_point.port))
        intro.append("onion-key")
        intro.append(intro_point.onion_key)
        intro.append("service-key")
        intro.append(intro_point.service_key)

    intro_section = '\n'.join(intro).encode('utf-8')
    intro_section_base64 = base64.b64encode(intro_section).decode('utf-8')
    intro_section_base64 = textwrap.fill(intro_section_base64, 64)

    # Add the header and footer:
    intro_points_with_headers = '\n'.join([
        '-----BEGIN MESSAGE-----',
        intro_section_base64,
        '-----END MESSAGE-----'])
    return intro_points_with_headers


def make_public_key_block(key):
    """
    Get ASN.1 representation of public key, base64 and add headers
    """
    asn1_pub = util.get_asn1_sequence(key)
    pub_base64 = base64.b64encode(asn1_pub).decode('utf-8')
    pub_base64 = textwrap.fill(pub_base64, 64)

    # Add the header and footer:
    pub_with_headers = '\n'.join([
        '-----BEGIN RSA PUBLIC KEY-----',
        pub_base64,
        '-----END RSA PUBLIC KEY-----'])
    return pub_with_headers


def sign_digest(digest, onion_address):
    """
    Sign, base64 encode, wrap and add Tor signature headers

    The message digest is PKCS1 padded without the optional
    algorithmIdentifier section.
    """

    signature_bytes = Keycity().sign_please(digest, onion_address)
    signature_base64 = base64.b64encode(signature_bytes).decode('utf-8')
    signature_base64 = textwrap.fill(signature_base64, 64)

    # Add the header and footer:
    signature_with_headers = '\n'.join([
        '-----BEGIN SIGNATURE-----',
        signature_base64,
        '-----END SIGNATURE-----'])
    return signature_with_headers


def sign_descriptor(descriptor, onion_address):
    """
    Sign or resign a provided hidden service descriptor
    """
    token_descriptor_signature = '\nsignature\n'

    # Remove signature block if it exists
    if token_descriptor_signature in descriptor:
        descriptor = descriptor[:descriptor.find(token_descriptor_signature)
                                + len(token_descriptor_signature)]
    else:
        descriptor = descriptor.strip() + token_descriptor_signature

    descriptor_digest = hashlib.sha1(descriptor.encode('utf-8')).digest()
    signature_with_headers = sign_digest(descriptor_digest, onion_address)
    return descriptor + signature_with_headers


def descriptor_received(descriptor_content):
    """
    Process onion service descriptors retrieved from the HSDir system or
    received directly over the metadata channel.
    """

    try:
        parsed_descriptor = stem.descriptor.hidden_service_descriptor.\
            HiddenServiceDescriptor(descriptor_content, validate=True)
    except ValueError:
        logger.exception("Received an invalid service descriptor.")
        return None

    # Ensure the received descriptor matches the requested descriptor
    permanent_key = Crypto.PublicKey.RSA.importKey(
        parsed_descriptor.permanent_key)
    descriptor_onion_address = util.calc_onion_address(permanent_key)

    # Find the HS instance for this descriptor
    for service in config.services:
        for instance in service.instances:
            if instance.onion_address == descriptor_onion_address:
                # Update the descriptor and exit
                instance.update_descriptor(parsed_descriptor)
                return None

    # No matching service instance was found for the descriptor
    logger.debug("Received a descriptor for an unknown service:\n%s",
                 descriptor_content.decode('utf-8'))
    logger.warning("Received a descriptor with address %s.onion that "
                   "did not match any configured service instances.",
                   descriptor_onion_address)

    return None


def upload_descriptor(controller, signed_descriptor, hsdirs=None):
    """
    Upload descriptor via the Tor control port

    If no HSDir's are specified, Tor will upload to what it thinks are the
    responsible directories
    """
    logger.debug("Beginning service descriptor upload.")

    # Provide server fingerprints to control command if HSDirs are specified.
    if hsdirs:
        server_args = ' '.join([("SERVER={}".format(hsdir))
                                for hsdir in hsdirs])
    else:
        server_args = ""

    # Stem will insert the leading + and trailing '\r\n.\r\n'
    response = controller.msg("HSPOST %s\n%s" %
                              (server_args, signed_descriptor))

    (response_code, divider, response_content) = response.content()[0]
    if not response.is_ok():
        if response_code == "552":
            raise stem.InvalidRequest(response_code, response_content)
        else:
            raise stem.ProtocolError("HSPOST returned unexpected response "
                                     "code: %s\n%s" % (response_code,
                                                       response_content))
